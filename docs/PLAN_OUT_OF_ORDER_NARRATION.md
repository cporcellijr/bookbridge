# Plan: Align books whose EPUB order differs from the narration order (#426)

**Status:** Design-ready, not scheduled. Grounded in the shipped detector
(`map_quality.detect_out_of_order_blocks`, commit `1961e6a`) and the live
Four Past Midnight data.

**For:** A future implementation session. Phases 0-2 are independently landable;
Phase 3 is optional.

**Prior art:** Storyteller's author solved the same problem and wrote it up —
<https://smoores.dev/post/automating_immersive_reading/>. Read it first. Their
headline example is ours: in *Tress of the Emerald Sea* the acknowledgements open
the ebook but close the audiobook. We are not reinventing this.

---

## Root cause

`AlignmentService._filter_monotonic_lis` (line ~664) keeps the longest **strictly
increasing** subsequence of candidate anchors. When the audiobook narrates the
book's sections in a different order than the EPUB spines them, correct anchors
from all but one or two sections cannot chain onto that subsequence and are
discarded wholesale.

Four Past Midnight (EPUB spines the four novellas 2-4-3-1, audio narrates
1-2-3-4): **103,503 of 202,187 candidate anchors dropped (51%)**. Nothing was
missing from the audio — every novella is narrated. The anchors were correct and
unusable only because a single global monotonic chain cannot hold them.

### Validated on a second, milder book: Tress of the Emerald Sea

Run 2026-09-10 (`bookorbit:5849`, 618,629 chars, 12.45 h audio, `align_method=ctc`,
**quality 0.986**). This is the upstream author's own example and it reproduces the
same defect at 1/60th the scale, with the M4B cue sheet as exact ground truth.

The EPUB spines `ack.xhtml` at index 6 (chars 1,598-6,173); the audio narrates
Acknowledgments at **44,415.0-44,694.1 s**, second-to-last. The LIS dropped **611
of 73,632** candidates (0.83%) - almost exactly the anchor count that 4,575 chars
of acknowledgements generates.

Both directions are wrong, and they are **the same event seen from two sides**:

| Direction | Map says | Truth | Error |
|---|---|---|---|
| char 3,000 (mid-ack) | **10.7 s** | 44,415 s | **12.3 hours** |
| ts 44,415-44,694 s (ack audio) | chars 615,043-615,280 | chars 1,598-6,173 | stalls at **0.85 chars/s** vs 13.8 book-wide |

The second row is what looked like a `with_star=False` weakness (audio with no
text). It is not: the audio *does* have matching text, 600 k chars away. **Correct
segment placement fixes both rows at once** - the phantom-audio symptom is
downstream of the misplacement, not independent of it. Genuinely text-less audio
(Opening/Closing Credits, ~60 s total here) remains a separate, minor question.

Three consequences for this plan:

1. **Chapter metadata is ground truth and a free boundary search.** The `.cue`
   listed all 77 audio sections with timestamps; BookOrbit reported the same as 77
   markers. Where audio chapter titles exist, matching them to spine titles places
   segments directly - RANSAC is the fallback for books without usable metadata,
   not the primary mechanism. This is cheaper and more reliable than the post's
   approach for the large fraction of audiobooks that carry chapter marks.
2. **Tress is the regression fixture.** Small, real, with known-correct answers
   from the cue sheet: the ack segment must land at 44,415 s, and the tail stall
   must disappear.
3. **The quality score cannot see this.** 0.986, `max_gap_fraction` 0.010,
   `density_spread` 1.199, `backwards_fraction` 0.0 - every metric healthy while
   1% of the book is 12 hours wrong. See the scorer gap in Phase 3.

### Validated on real Four Past Midnight anchors (2026-09-10)

The case the plan was written for, measured end to end on real data rather than a
synthetic permutation. Captured the actual candidate anchors by intercepting the
LIS filter (`bookorbit:5417`, 1,602,700 chars, 14 non-empty spine boundaries,
201,898 candidates) and ran `fit_segments` over them:

| | global LIS (today) | segment fitting |
|---|---|---|
| anchors retained | 97,706 (**48.4%**) | 195,905 (**97.0%**) |
| boundaries placed | - | 12 of 14 |
| ts-overlapping pairs | - | 0, `_assert_disjoint` passes |

The recovered narration order is a genuine permutation: chars 1,105,949-1,602,591
are narrated first, then 11,947-333,320, then 677,136-1,105,948, with
333,321-677,135 last. The two unplaced boundaries are front matter and a 108-char
tail - correctly rejected. Its stored CTC map scores **0.323**, the worst on the
install, which is the broken-map signature this work exists to remove.

**Known blemish, not yet fixed:** the first placed segment reports
`ts_start = -175.1`. That is the fitted line extrapolated below zero at
`char_start`, and it is *meaningful* - the audio opens with ~175s of credits that
have no ebook text - but a negative timestamp has no business being persisted.
Clamping is not free: `select_anchors` derives its line from the segment's own two
edges, so clamping an edge skews that line. Not reachable from `get_progress_for_time`
(which divides char, not ts), so it is latent rather than live. Resolve it in Phase 3.

Residuals here run 12-48s against Tress's 0.7-5s, which is proportionate: these
segments are 100k+ chars and 7,000+ seconds long, so 40s is about 0.5%.

### Why the LIS is not simply wrong

Both public lookups binary-search **the same list**:

- `get_time_for_text` (line 505; the search runs at ~line 526) binary-searches
  assuming `char` ascends.
- `_interpolate_char_for_time` (line 582, reached via `get_char_for_time` at line
  572) binary-searches assuming `ts` ascends.

One flat list can serve both only if it is monotonic in *both* keys. The LIS is
what makes those two searches valid. **Removing it without changing the map
contract silently corrupts every lookup** — binary search over unsorted data
returns a wrong answer, not an error. This is the whole risk of the work.

### What is already right

Our anchor *generation* already matches Storyteller's: `_find_anchors` builds
n-grams (`_LEXICAL_ANCHOR_WORDS = 12` words; theirs is 10 characters) and keeps a
match only when the key is unique on **both** sides — the same "globally unique
matches" criterion. We diverge only at the consensus step: we apply a global LIS,
they apply **RANSAC** per chapter over a local linear fit.

So this is not a rewrite of alignment. It is a replacement of one filter, plus the
map-format change that makes the replacement expressible.

---

## Design

Reuse the candidate anchors we already compute. Instead of forcing them into one
global chain, fit each ebook segment to the audio independently, then order the
segments by where they actually landed.

```
today:   anchors ──> global LIS ──────────────> flat monotonic map
planned: anchors ──> per-segment RANSAC fit ──> segments ──> flat map + segment index
                       └─ unplaced segments dropped (genuinely unnarrated)
```

Segments are spine chapters (`spine_chapters` is already threaded to the CTC call
sites). Chapterless books fall back to fixed char windows and, failing that, to
today's single-segment behaviour.

---

## Phase 0 — Segment fitting (pure, no format change, no behaviour change)

New pure module `src/services/segment_fit.py`, unit-testable without DB or torch,
in the style of `map_quality.py`.

```python
def fit_segments(anchors: List[Dict], boundaries: List[Tuple[int, int]],
                 total_chars: int) -> List[Segment]
```

**Try chapter metadata first.** When the audio exposes chapter marks (M4B/cue,
BookOrbit markers, ABS chapters) and their titles can be matched to spine section
titles, that placement is authoritative - skip RANSAC for those segments. Fall
through to the fit below for unmatched segments and for books with no marks.

For each `(char_start, char_end)` boundary:

1. Take candidate anchors whose `char` falls inside it. (No new n-gram work —
   these are the same `anchors` the LIS already receives.)
2. RANSAC a linear `ts = a*char + b` over them: sample pairs, count inliers within
   a residual tolerance, keep the best consensus set. **Use a fixed seed** — maps
   must be reproducible across runs or the `_publish_map` regression veto compares
   noise.
3. Reject the segment when inliers are too few, the fit slope is non-positive, or
   the implied chars/sec is outside a sane band. A rejected segment is *unplaced*,
   not an error — front matter, TOC and appendices legitimately have no audio.
4. Return `Segment(char_start, char_end, ts_start, ts_end, inliers, residual)`.

Then resolve conflicts: sort placed segments by `ts_start`; where two claim
overlapping audio, keep the stronger fit (more inliers, lower residual) and demote
the other to unplaced.

**Invariant this must guarantee, asserted in code:** placed segments are pairwise
disjoint in `char` (true by construction) **and** in `ts` (true after conflict
resolution). Everything downstream depends on it. Reordering does not break
disjointness — it only breaks the correlation between the two orders.

**Acceptance:**
- A normal in-order book yields exactly **one** placed segment spanning the book.
- Synthetic 4-block permutation (2-4-3-1) yields 4 placed segments in narration
  order with ~all anchors retained.
- Determinism: same input produces byte-identical output across 100 runs.
- Nothing calls it yet; the suite is unchanged.

---

## Phase 1 — Map format and lookups (the contract change)

Additive migration: new nullable `segments_json` column on `book_alignments`
(`[{char_start, char_end, ts_start, ts_end}]`). `alignment_map_json` keeps its
exact current shape — a flat list sorted by `char` — so every existing reader,
including KoSync consumers, is untouched.

`_get_alignment` (~line 1348, LRU-cached) gains a ts-sorted segment index built
once at load.

- **char to ts**: binary search on `char` as today. One change: **never
  interpolate across a segment boundary.** Clamp to the segment edge instead.
  Interpolating from the end of novella 4 to the start of novella 1 is precisely
  what produced "22% of the text inside 23 seconds".
- **ts to char**: when segments exist, locate the segment by `ts` first (they are
  ts-disjoint), then binary search within that segment's slice.
- **`segments_json IS NULL` means today's code path, unchanged.** This is the
  compatibility guarantee and the rollback story: 372 live maps keep working
  untouched.

**Acceptance:**
- Every existing alignment test passes unchanged (they all have NULL segments).
- A hand-built 2-segment reordered map round-trips correctly in both directions.
- Falsification: remove the boundary clamp and a test asserting a position inside
  the gap between two segments must fail.
- Migration applies base to head on a fresh SQLite (in-container recipe: copy
  `alembic.ini`, rewrite `script_location` to `/app/alembic` and `sqlalchemy.url`
  to a temp path, then `alembic -c` upgrade).

---

## Phase 2 — Wire it in behind a setting

New boolean `ALIGNMENT_SEGMENTED_MAPS`, **default false**. Follow the
`/add-setting` skill — `ALL_SETTINGS`, `DEFAULT_CONFIG`, `bool_keys`,
`settings.html` — and read it per call via `env_truthy` (failure modes #1 and #3).

When on, `_generate_alignment_map_with_method` calls `fit_segments` and emits the
flat map plus `segments_json`; when off, or when fitting yields a single segment,
behaviour is bit-identical to today.

`detect_out_of_order_blocks` stops being only a warning: a reordered book that
fitted cleanly logs a resolved, informational line naming the narration order.
Keep the existing WARNING for books where fitting *failed* — the log text is a
contract (§5), so add a suffix, never rewrite the prefix.

**Acceptance (live, not unit):** re-align Four Past Midnight with the flag on.
- Retained anchors ~98,684 rises toward ~200,000.
- Quality score rises; `max_gap_fraction` collapses.
- Spot-check real positions in each of the four novellas against the audio.
- A control book re-aligned with the flag on produces a **byte-identical** map.

---

## Phase 3 — Segment-aware quality (done; the veto premise was false)

**Implemented 2026-09-10. The stated premise above was checked against real
data and found false — it is corrected here, not implemented as written.**
The plan said the regression veto would refuse the improved segmented map
unless Phase 3 landed first. Measured directly:
`is_regression(incumbent=LIS map, challenger=segmented map)` returns **False**
on the real Four Past Midnight maps (201,898 candidate anchors, 14 spine
boundaries) — the segmented map (0.7781) clears the LIS map (0.2000) and the
0.75 realign threshold with no Phase-3 change at all. There is no ordering
hazard, and Phase 3 did not need to land before, with, or after any Phase 2
default flip.

The real problem was narrower and still worth fixing: `density_spread`
(`map_quality._density_spread`) measures across the whole map, so it charges
a *correctly* segmented reordered book for the `ts` discontinuities at its
own segment seams — real audio structure, not measurement error. Scored
per-segment instead, the same Four Past Midnight map is excellent everywhere
(per-segment `density_spread` max 1.327, median 1.146 across the 12 placed
segments) against a whole-map figure of 5.394 — a ~4x inflation purely from
seam boundaries. That left a genuinely excellent map scoring only +0.0281
above the realign threshold: fragile enough that a slightly worse book would
fall below it and get flagged for a pointless re-alignment.

**What shipped** (the `max` aggregation below was later measured to be
scale-dependent and replaced — see "Phase 3 corrected" further down; the rest of
this list still stands)**:**
- `map_quality.score_map` gained an optional `segments` parameter. When
  supplied (non-empty), `density_spread` is computed independently over each
  segment's own char slice and aggregated with **`max`** — one badly-paced
  segment still drags the score down; averaging would let it hide behind
  the others (`_segment_aware_density_spread`, `_segment_bounds`,
  `_segment_slice_points`). `segments=None` (or `[]`) is byte-identical to
  the pre-Phase-3 code path — the compatibility guarantee for the 372
  already-stored, non-segmented maps.
- `max_gap_fraction` deliberately stays whole-map — **not** made
  segment-aware. Verified directly: per-segment gap fractions (0.002-0.02)
  agree with the whole-map figure (0.0221) on the real data, because placed
  segments tile the char space contiguously and a char gap is real
  regardless of which segment it falls in. Making it segment-aware would
  have been churn with no behavioral difference.
- `AlignmentService._publish_map` threads each side's own segments into its
  own `score_map` call — the challenger's `segments` argument, and the
  incumbent's via `_get_segments(abs_id)` — never each other's, since the
  two maps can disagree on whether they're segmented at all.
- `AlignmentService._segments_to_json` clamps `ts_start` to 0 at the
  persistence boundary (real Four Past Midnight data: -175.1s for the first
  placed segment, meaning ~175s of opening credits with no matching ebook
  text). The in-memory `Segment` stays unclamped: `select_anchors` derives
  its line from the segment's own two edges, and clamping there would skew
  that line's slope.

**Investigated and deliberately NOT built: `max_time_gap_fraction`.** The
plan proposed this metric because Tress parked for 279s while advancing 237
chars (0.85 chars/sec) with every other metric healthy. Verified directly on
real data: segmentation removes that stall entirely — the acknowledgements
segment now covers that same audio at 16.7 chars/sec once it is placed at
its correct position instead of interpolated across a 12-hour gap. The
metric would guard a symptom that segmentation already eliminates as a side
effect, so it would be built to catch nothing. Left unbuilt (same trap as
the guard shipped and then deleted earlier in this work for being provably
inert) — this note is the record of why, should the question come up again.

**Not done, still future work if ever prioritized:** the plan's other Phase
3 idea — feeding per-segment audio windows into
`ForcedAligner._chunked_word_times` so CTC on reordered books improves for
free — was not touched by this pass. `AlignmentService.align_forced_and_store`
still passes the flat lexical map's `boundaries`, not the segment index, to
`ForcedAligner.align`. Scope for this pass was the scorer and the
persistence clamp only.

### Phase 3 corrected: `max`-of-per-segment was scale-dependent (2026-09-10)

**The `max` aggregation above was wrong, and this section supersedes it.** The
diagnosis of seam contamination was right; the remedy changed the *statistical
scale* the metric runs at, which is a second bug the first one hid.

`_density_spread` cuts a map into `_DENSITY_SLICE_COUNT` (20) equal-anchor-count
slices. Its band (`_DENSITY_SPREAD_GOOD` 2.0, `_DENSITY_SPREAD_BAD` 10.0, weight
0.45) is calibrated for whole-map runs, where one slice holds thousands of anchors
across hundreds of seconds. Run per segment on a finely-spined EPUB, a slice holds
about **nine anchors across about five seconds** — a scale at which local narration
jitter and transcript timing artifacts, not pacing, dominate the statistic. `max`
then gives the single worst artifact anywhere in the book a veto over 45% of the
score, with no weighting by how much of the book that segment covers.

**Measured on The Terminal Man** (`5b9770d6…`, a 237-document EPUB, 226 placed
segments): flat-scored the segmented map is **0.9926**; segment-scored it is
**0.5426**. It is the better map on every scale-independent axis — max gap fraction
0.0342 → 0.0053, whole-map `density_spread` 1.414 → 1.106, 186 more retained
anchors. The per-segment spreads are *tight* — median 1.548, p90 2.112 — with one
value at **70.796**. That segment covers 0.36% of the book, and one of its slices
holds nine anchors spanning 59 chars in **0.08 seconds**: a transcript artifact on
a book whose transcript carries `0 measured, 57328 estimated` word timings.

**This is not the max-of-N-noisy-estimates effect it resembles.** The proof is
independent of segmentation entirely: 355 undisputed **in-order** stored maps,
unchanged, re-measured with `k` equal char slices as synthetic segments. Maps whose
density sub-score falls below 0.5 — k=1: **0**, k=5: 20, k=20: 64, k=50: **117**,
k=226: 76. The map never changes; only how it is measured. The 60-point floor added
earlier does not address this, because 60 points is still only three per slice.

It already affects shipped books: Tress's live segmented map measures **2.262**
under this metric (whole-map 1.092), past `_DENSITY_SPREAD_GOOD` and losing score;
Dearest measures 1.910.

**What replaced it.** `_segment_aware_density_spread` keeps whole-map 20-slice
granularity and builds each slice's rate by summing consecutive point deltas,
skipping any pair that does not lie wholly inside one placed segment. That removes
seam contamination — Phase 3's actual goal — without moving the scale.

- **Telescoping equivalence:** `Σ(c[i+1] − c[i]) == c_last − c_first`, so when no
  pair is skipped this reproduces `_density_spread` *exactly*, not approximately
  (verified to 1e-9 on six real maps). The compatibility guarantee for the stored
  non-segmented maps therefore holds by construction, not by a branch.
- Four Past Midnight segmented **5.394 → 1.201** (the `max` version gave 1.327, so
  the replacement does the seam job at least as well); The Terminal Man 70.796 →
  1.060; Tress 2.262 → 1.048; Dearest 1.910 → 1.064.
- **Negative control:** Four Past Midnight's broken LIS map stays at **11.386**
  (score 0.2000). It does not launder a genuinely bad map.
- Across the 355-map granularity sweep: median 1.13 → 1.08, and **zero** maps fall
  below half density score at any `k`.

A badly-paced segment still lowers the score — now in proportion to how much of the
book it covers, which is the property the `max` version was missing. Segments
arrive sorted by `ts_start` (not by char), so membership is resolved by bisect over
a char-sorted copy; `_segment_slice_points` and `_SEGMENT_MIN_POINTS_FOR_DENSITY`
are gone with the per-segment path they served.

### Scope limit found in the wild: narration that interleaves endnotes (2026-09-10)

A full-library sweep found 15 of 324 books (4.6%) narrated out of spine order. One,
**Eaters of the Dead**, is a genuine regression under segmentation, and it is worth
recording because it is the plan's stated scope limit meeting a real book.

It places only **22 of 84** boundaries. Of the 62 unplaced, 24 hold too few
candidates — and **38 pass their own RANSAC fit and are then dropped by
`_resolve_conflicts`**. Every one of the 38 lies in chars 261k–291k (Crichton's
endnote apparatus) and fits to audio sitting *inside* a main-body chapter's range:
chars 268299–272954 (537 inliers) fits ts 9301–9601, inside the chapter spanning
chars 101095–132503 at ts 8039–10460. The audiobook narrates the footnotes
**inline, where they are referenced**, so chapter and endnote genuinely share the
same audio. Conflict resolution is behaving correctly — the model simply cannot
express "both, alternating," which is finer than the chapter granularity this plan
declares as its limit.

Cost, measured with held-out candidate anchors (see "Arbitrating two maps" below):
`cov@30s` **0.908 → 0.803**, p90 error **0.59 s → 294.57 s**, while gross errors
(0.092 vs 0.088) and worst case (5.34 h both) are a wash. Segmentation degrades a
tenth of the book by minutes and fixes nothing. Note both maps are ~9% grossly
wrong: this book is broken either way, and 12.5% of its char space ends up with no
segment coverage, so lookups there clamp to a segment edge.

**No gate was added.** Three signals separate it cleanly from every book
segmentation helps — boundaries placed 26% vs ≥95%; anchors retained versus the LIS
0.807 vs 0.977–1.042 across all 15 out-of-order books; placed char coverage 0.874
vs ≥0.960. But that is one negative example, and a gate calibrated on n=1 is a
guess wearing a threshold. Documented as a known limit instead; revisit if a second
interleaved-narration book turns up.

### Arbitrating two maps: held-out candidate-anchor agreement (2026-09-10)

`map_quality.score_map` inspects a map's own internal shape and never compares it
against positional evidence, so it **cannot see out-of-order damage** — it reported
"LIS wins" on Tress while that LIS map was provably 12.3 hours wrong. Scoring two
maps with it to pick a winner is circular. The non-circular substitute:

Split the candidate anchors by index parity over the char-sorted list, rebuild each
map from the **even** half only, and grade both on the **odd** half — predicting ts
with `get_time_for_text`'s exact semantics, segment clamp included. Neither map has
seen the evaluation set.

Validated against the four books whose truth was already established positionally,
*before* being trusted on any unknown book:

| book | LIS `cov@30s` / gross / worst | segmented |
|---|---|---|
| Four Past Midnight | 0.487 / 0.513 / **23.4 h** | 0.971 / 0.018 / 0.56 h |
| Tress | 0.996 / 0.004 / 12.4 h | 1.000 / 0.000 / 0.08 h |
| Dearest | 0.999 / 0.001 / 8.4 h | 1.000 / 0.000 / 0.01 h |
| Animals (control) | 1.000 / 0.000 | identical — inert |
| The Terminal Man | 0.995 / 0.005 / **7.2 h** | 1.000 / 0.000 / 0.008 h |
| Eaters of the Dead | **0.908** / 0.092 / 5.34 h | 0.803 / 0.088 / 5.34 h |

Honest limits: the evaluation anchors come from the same n-gram matcher, so a
section with no unique 12-grams is invisible to both maps; false anchors penalise a
correct map, so read the median and the gross fraction, never the mean; and it
compares anchor *selection*, not the full production pipeline.

### Known limitation / future work: CTC is segment-unaware (2026-09-10)

CTC was re-enabled globally while still blind to segments. Measured on Four
Past Midnight (`bookorbit:5417`): with segmented placement on and CTC off, it
scored **0.9690** with 12 persisted segments, correct in both lookup
directions. Its previous CTC map scored **0.3230**. The gap is
`_chunked_word_times`: it derives each chunk's audio window from the
incumbent lexical map's char->ts anchors, and for a reordered book that
mapping is not monotonic across segment boundaries, so the windows are
wrong — CTC then overwrites the good segmented map with its own worse one.

**Interim answer, shipped:** a guard in `align_forced_and_store`
(`AlignmentService._get_segments(abs_id)` non-empty) refuses the CTC pass
outright and returns `False` — its documented "cannot run, caller falls back
to the lexical pipeline" contract — so the caller keeps the existing
segmented map untouched. A brand-new book (no stored map yet) has no
segments to find, so this never blocks a first alignment; only the
upgrade/remap path on an already-segmented book is refused. This is
deliberately not a fix: it trades a rare book's CTC upgrade for keeping the
good map it already has.

**Eventual fix, not built:** teach `_chunked_word_times` (or a new
segment-aware sibling) to decode and align each placed segment's own audio
range independently, using that segment's own char->ts edges as its window
instead of the flat map's. That is real per-segment CTC chunking, a
materially larger change than this guard, and remains open scope.

---

## Known refinement: sparsely-anchored segments clamp instead of interpolating

Found in the live Dearest run (2026-09-10). A placed segment carries a fitted
line, but lookups inside it still use only the flat map's own anchors. When a
segment is sparsely anchored the two disagree.

Dearest's displaced block spans chars 2-1,807 and is placed at ts
30,361.6-30,465.1 - the correct audio window. But the n-gram matcher found only
**32 anchors** in it, covering chars 311-614 and ts 30,379-30,397. So a reverse
lookup above ts 30,396.6 clamps to char 614, when the fitted line puts ts 30,450
at char ~1,543: a **~930-char error**, roughly a minute of narration, over 0.4%
of the book. Forward lookups outside 311-614 likewise return the segment's
clamped edges rather than interpolated positions.

This is sparse candidate data, not a fault in the placement: a 1,805-char
section yields few 12-word n-grams unique on both sides. The placement itself is
right, and the block went from **8.4 hours** wrong to ~1 minute wrong.

**The fix, when it is worth doing:** inside a placed segment, bound
interpolation by the segment's own `(char_start, ts_start)` and
`(char_end, ts_end)` rather than by its outermost anchor - the fitted line is
already stored and is better evidence than "nearest anchor" out at the edges.
Deliberately not done in this pass: it changes interpolation semantics for every
segmented book, and the residual it removes is small next to what segmentation
already fixed.

## Risks

| Risk | Mitigation |
|---|---|
| Binary search over a non-monotonic list returns wrong positions **silently** | Phase 1 lands before any producer writes segments; NULL means the old path |
| RANSAC non-determinism churns maps and defeats the regression veto | Fixed seed; determinism test in Phase 0 acceptance |
| A wrong segment fit claims another segment's audio | Conflict resolution plus the asserted ts-disjointness invariant |
| Quality scorer rejects the improved map | Measured false: `is_regression` already accepts the segmented map without Phase 3 (see Phase 3 above). Phase 3 shipped anyway, for scoring accuracy, not to unblock this. |
| Reordering *within* a chapter | Out of scope. Chapter granularity is the stated limit |

## Per-segment CTC chunking: considered and declined

Recorded 2026-09-10 so it is not re-proposed blind. CTC stands down on segmented
books (the phase 4 guard) because `_interp_ts` assumes char and ts ascend together.
Teaching CTC to chunk per segment would lift that restriction.

Measured anchor density says it is not worth it:

| map | method | chars per anchor |
|---|---|---|
| Four Past Midnight, segmented | lexical | 8.2 |
| Four Past Midnight, old CTC map | ctc | 7.1 |
| Dearest, segmented | lexical_timed | 7.6 |
| Animals (in-order control) | ctc | 5.7 |

An English word averages about 5 characters, so both backends already anchor below
word granularity; the CTC onset advantage was separately measured at ~1 char. The
entire gain is interpolation distance falling from ~8 chars to ~6, on three books
whose positions are already correct — against restructuring the chunking code that
every earlier CTC defect in this issue lived in.

The guard is therefore the permanent answer, not a placeholder.

## Scope check before starting

**1 of 372 maps on the primary install.** This is a correctness fix for a rare
book, not a throughput win. If it competes with anything user-facing, it loses.
The shipped detector means these books are now *diagnosed* rather than silently
wrong, which was the urgent half.
