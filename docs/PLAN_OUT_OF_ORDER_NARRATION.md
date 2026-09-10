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

## Phase 3 — Optional: segment-aware quality, and CTC windows

`map_quality._density_spread` (line 166) and `max_gap_fraction` (line 112) measure across the whole map
and would score a *correctly* segmented reordered book as damaged. Make both
segment-aware (compute per segment, aggregate) once Phase 2 lands, or the
regression veto in `_publish_map` will refuse the improved map.

Note the ordering hazard: **Phase 3 must land with or before Phase 2's default
flip**, never after.

Separately, per-segment audio windows are exactly the chunk bounds
`ForcedAligner._chunked_word_times` already wants, so CTC on reordered books
improves for free.

---

## Risks

| Risk | Mitigation |
|---|---|
| Binary search over a non-monotonic list returns wrong positions **silently** | Phase 1 lands before any producer writes segments; NULL means the old path |
| RANSAC non-determinism churns maps and defeats the regression veto | Fixed seed; determinism test in Phase 0 acceptance |
| A wrong segment fit claims another segment's audio | Conflict resolution plus the asserted ts-disjointness invariant |
| Quality scorer rejects the improved map | Phase 3 before the default flip |
| Reordering *within* a chapter | Out of scope. Chapter granularity is the stated limit |

## Scope check before starting

**1 of 372 maps on the primary install.** This is a correctness fix for a rare
book, not a throughput win. If it competes with anything user-facing, it loses.
The shipped detector means these books are now *diagnosed* rather than silently
wrong, which was the urgent half.
