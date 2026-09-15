# Plan: Option C — Recover chunk-skipped regions with a second CTC pass (#426)

**Status:** Handoff-ready. Grounded in commits `7ffc966` (mid-chapter) + `a8b1ce6` (item 3). No dependencies on unmerged branches.

**For:** Codex or Haiku implementation. Can be handed off or implemented inline.

---

## Root cause (why C works)

`ForcedAligner._chunked_word_times` derives each chunk's audio window from the **lexical map** (`boundaries`) via `_interp_ts`. When a region's lexical timestamps are compressed (e.g. ~1,600 words mapped to ~32 s), the window is smaller than the token count, so the guard `if f_hi - f_lo <= seg_tokens:` skips the chunk → that span is interpolated. The audio is actually there (region is narrated; truly-unnarrated spans were already removed upstream by `_detect_unnarrated_spans`). C re-windows those spans from the **first pass's own CTC anchor times**, which are reliable, and proportionally sub-chunks across that true time span.

---

## Design (two passes inside `_chunked_word_times`)

Pass 1 = today's loop, but record per-word times so skipped runs and their reliable bracketing anchors are recoverable. Pass 2 = for each skipped run, align it inside `[ts_before, ts_after]` taken from Pass 1 results.

---

## Phase 1 — Instrument Pass 1 (small, no behavior change)

Replace the ad-hoc `results` accumulation with a dense `word_ts: List[Optional[float]] = [None] * n`. In the aligned branch set `word_ts[i+k] = ts`; in the skip/fail branches leave `None`. Keep the existing skipped/failed counters and the summary WARNING (item 3).

Build `results` at the end from `word_ts` (`[(kept[k][1], word_ts[k]) for k if word_ts[k] is not None]`).

**Acceptance:** existing chunked tests pass unchanged (byte-identical maps when nothing is skipped). No second pass yet.

---

## Phase 2 — Second pass over skipped runs

Add `_recover_skipped_spans(F, torch, emission, kept, word_tokens, seconds_per_frame, word_ts)` called after Pass 1, before building `results`. Algorithm:

1. Find maximal runs of consecutive `None` in `word_ts` → `[i, j)` word-index ranges.
2. For each run: `a = last index < i with word_ts not None`; `b = first index ≥ j with word_ts not None`. If either is missing (run touches book start/end), skip recovery (leave interpolated).
3. `fa = word_ts[a] / spf`, `fb = word_ts[b] / spf`. `T = sum(len(word_tokens[k]) for k in range(i, j))`. If `fb - fa <= T` → genuinely too little audio (likely truly unnarrated) → leave interpolated. *(This is C's built-in cram guard: no real audio ⇒ no forced alignment.)*
4. Sub-chunk `[i, j)` by `_MAX_CHUNK_TOKENS`. For sub-chunk `[p, q)` with cumulative token offsets `c0..c1` within the run, allocate a **proportional** window `wlo = fa + (fb-fa)*c0/T - margin`, `whi = fa + (fb-fa)*c1/T + margin` (clamp to `[max(0,fa-margin), min(total_frames, fb+margin)]`). If `whi - wlo <= (c1-c0)` still, or `_single_pass_fits` is violated, split further / skip that sub-chunk.
5. `times = self._segment_word_times(F, torch, emission[:, wlo:whi], word_tokens[p:q], spf, frame_offset=wlo)`; on success write `word_ts[p+k] = t`.

**Acceptance:** a new unit test (below) shows a compressed-boundary region that Pass 1 skips is recovered by Pass 2 to the correct frames; the no-skip case is untouched.

---

## Phase 3 — Guards, monotonicity, provenance

- **Monotonicity:** `_stitch_char_times` already drops ts inversions at seams — keep relying on it as the backstop, but Pass 2 should produce monotonic times by construction (proportional windows are ordered).
- **One recovery pass only** (no looping). Cap total Pass-2 work (e.g. bail out of recovery if a run's `T` or `fb-fa` implies a cost over `_single_pass_fits` even after max sub-chunking — leave interpolated).
- **No new `align_method`**; still `'ctc'`. No change to `align()`'s caller contract or the acceptance gate.
- **Interaction with `exclude_spans`:** Pass 2 only touches `None` runs that are **not** inside an excluded span (excluded words were never added to `kept`, so they can't appear as runs — verify with a test that a book with both an excluded span and a compressed region handles each correctly).

---

## Phase 4 — Tests (`tests/test_forced_aligner.py`)

Copy the synthetic-emission harness from `test_chunked_align_covers_whole_book_via_boundaries`. Add:

1. **Recovery:** build 26 single-letter "words"; give `boundaries` correct times **except** compress a middle run (map its chars into a tiny ts range) so Pass 1 skips those chunks; assert the returned map (a) has anchors for the middle letters at their true frames (±tolerance), (b) is monotonic, (c) equals the all-correct result within tolerance. Force small `_MAX_CHUNK_TOKENS` (as the existing test does) to exercise sub-chunking.

2. **Truly-unnarrated stays a gap:** a run where `fb - fa <= T` (no audio) → those words remain unaligned (interpolated), not crammed.

3. **No-op:** a book with no skipped chunks returns the exact Pass-1 result (guard against regressions).

4. **Exclusion + compression together:** one excluded span and one compressed region in the same book; excluded span stays a gap, compressed region is recovered.

**Acceptance:** full suite green (`pytest tests/ -q`; current baseline **3734 passed, 9 skipped**), no test weakened.

---

## Phase 5 — Live verification & deploy

- **In-container** (bind-mounted `src`, fresh `python3` picks up edits): remap **Four Past Midnight** (`bookorbit`/abs_id `56126442-75fd-462e-83ae-66d743880f41`) and confirm the middle region (words ~62k–124k) now gets anchors — the skip summary count drops sharply and `_max_gap_fraction` of the stored map falls below today's 0.210.
- Confirm the acceptance gate still accepts (should be strictly better) and the backup/rollback path is intact.
- Re-run the 356-map detection sweep is **not** needed (C doesn't touch detection), but spot-check a couple of previously-clean books remap identically (no new gaps).
- **Deploy:** `docker compose restart` (src-only; no migration).

---

## Risks & mitigations

- **Cramming a genuinely-unnarrated run** → prevented by Phase 3's `fb - fa <= T` guard (no audio ⇒ no alignment) and by `_detect_unnarrated_spans` already removing real gaps upstream.
- **Wrong proportional split within a run** → forced_align refines onsets within each sub-window; a generous `margin` absorbs the proportional estimate's error; `_stitch_char_times` drops any residual inversion.
- **Cost blowup on a slow-narrated long run** → per-sub-chunk `_single_pass_fits` check + further splitting.

---

## Handoff notes

- Treat this as **additive to `_chunked_word_times`** (Phase 1 refactor + one helper).
- Must run the full suite plus the in-container Four Past Midnight remap before calling it done — the mid-chapter work proved that only a real remap surfaces the timing-edge cases.
- All phases are self-contained; Phase 1 can land separately (no-op behavior change) and Phase 2 can follow.

