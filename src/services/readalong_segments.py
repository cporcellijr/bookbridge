"""Sentence segmentation and clip-time interpolation for read-along EPUB 3
generation.

Builds on Phase 1 (``src/utils/ebook_dom_map.py``): that module recovers DOM
provenance for every character offset in ``EbookParser.extract_text_and_map``'s
combined text; this module splits that same text into sentences -- scoped one
spine item at a time, since a SMIL ``<par>`` can never reference two XHTML
documents -- and interpolates each sentence's ``(char_start, char_end)``
through the book's alignment map to get an audio clip ``(ts_start, ts_end)``.

Sentence ids follow Storyteller's ``<chapter>-s<N>`` convention, confirmed
against the MIT-licensed ``storyteller-platform/storyteller`` source
(``libraries/align/src/align/ctc/mediaOverlay.ts``:
``id: index === 0 ? `${chapter.id}-s${sentenceRange.id}` : ...``, where the
integer resets to 0 for each chapter). BookBridge already depends on that
``-sN`` shape -- ``EbookParser.get_media_overlay_fragment_ids`` collects ids
out of existing SMIL, and a fragment it doesn't recognise collapses read-along
playback to the start of the chapter. Storyteller's ``chapter.id`` is the
EPUB manifest item's own ``id`` attribute; ``extract_text_and_map``'s
``spine_map`` does not carry that (only ``spine_index`` and ``href``), so this
module uses ``c<spine_index>`` in its place -- still deterministic (fixed by
EPUB spine order) and directly reversible to the ``spine_map``/
``SpineDomMap`` entry a later phase anchors into.

Sentence segmentation and the interpolation below are original code, not
ported from Storyteller. Storyteller's ``getSentenceRanges.ts`` solves a
harder problem this module doesn't have: error-aligning a noisy ASR
transcript against reference text via edit-distance search. BookBridge's own
alignment maps make that unnecessary -- they are already 5-10x finer than
what Storyteller itself writes into SMIL, so a
plain interpolation over an existing char/timestamp map is enough. Sentence
splitting here is a dependency-free regex scan (see ``requirements.txt`` --
Storyteller instead pulls in ``@echogarden/text-segmentation``, an npm
package with no Python equivalent already vendored in this repo; adding a new
Python dependency would move this change from a bind-mount restart to an
image rebuild for a problem stdlib regex already solves adequately).
Storyteller's ``enforceMonotonicAudioRanges`` clamp-rather-than-interpolate
philosophy for audio that runs backwards informed, but was not copied into,
the monotonic clamp in :func:`build_sentence_clips`.
"""
import logging
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union, TYPE_CHECKING

from src.services.alignment_service import _segment_for_char
from src.utils.ebook_dom_map import block_break_offsets

if TYPE_CHECKING:
    from src.services.alignment_service import AlignmentService
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

# Lowercase, dot-stripped abbreviations after which a "." must not be read as
# a sentence end. Common English titles, Latin abbreviations, units and
# calendar short forms -- not exhaustive, and it doesn't need to be: a missed
# abbreviation only produces one extra (still valid, just smaller) sentence,
# never a wrong one.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "sgt", "capt",
    "col", "gen", "lt", "cmdr", "rev", "hon", "esq", "rep", "sen", "gov",
    "vs", "etc", "eg", "ie", "cf", "al", "no", "nos", "vol", "vols",
    "fig", "figs", "pp", "approx", "inc", "ltd", "co", "corp", "dept",
    "univ", "assn", "bros", "ave", "blvd", "rd", "mt", "ft", "sq",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
    "mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun",
    "us", "uk", "un",
})

# A sentence-boundary candidate: one or more terminal punctuation marks,
# optionally followed by closing quotes/brackets (a quotation's closing mark
# lands *after* the period that ends the quoted sentence), then whitespace.
_BOUNDARY_RE = re.compile(
    r'(?P<punct>[.!?]+)(?P<quotes>[\'"‘’“”)\]]*)(?P<ws>\s+)'
)

# Characters that plausibly open a new sentence, checked on the character
# immediately following a candidate boundary's whitespace. Anything else
# (most commonly a lowercase letter -- a trailing clause, an ellipsis
# mid-thought) means the candidate is not a real boundary.
_SENTENCE_START_CHARS = frozenset('"\'‘’“”([{—–')

# How far a recorded `total_chars` may sit from the EPUB's current extracted
# length and still be treated as the same book. Deliberately smaller than a
# short sentence: extraction drift moves a handful of characters, a different
# edition moves thousands. See `_map_fits_epub` for the measurement behind it.
_TOTAL_CHARS_DRIFT_TOLERANCE = 16

# Sentinel for "no segment floor has been established yet" -- distinct from
# `None`, which is itself a valid, real segment-key value (a sentence whose
# chars land in a gap between segments, see `_segment_key_for_char`). Using a
# dedicated object means the very first sentence of a segmented book always
# triggers `build_sentence_clips`'s floor-reset branch, even when that first
# sentence's own segment key happens to be `None`.
_UNSET_SEGMENT = object()


def _segment_key_for_char(segments: List[Dict], char_start: int, char_end: int) -> object:
    """A hashable/comparable identity for whichever segment ``char_start``
    (falling back to ``char_end``) belongs to, or ``None`` if neither lands
    in any segment (a gap -- front/back matter with no narration
    correlation, typically).

    Used by :func:`build_sentence_clips` to detect when consecutive
    sentences cross a segment boundary, so its monotonic floor can reset
    instead of carrying a timestamp forward from audio the new segment has
    no ordering relationship with (Finding 3 of the independent review).
    Keyed on ``id()`` of the segment dict itself rather than its char/ts
    values: :meth:`AlignmentService._get_segments` returns the same list
    (and the same dicts) for the lifetime of one ``build_sentence_clips``
    call, so identity is stable and cheaper than a value comparison, and
    never ambiguous between two segments that might coincidentally share
    edge values.
    """
    segment = _segment_for_char(segments, char_start)
    if segment is None:
        segment = _segment_for_char(segments, char_end)
    return id(segment) if segment is not None else None


def _preceding_token(text: str, pos: int) -> str:
    """The run of alnum/period characters immediately before ``pos``.

    Includes internal periods so a multi-part abbreviation like ``e.g`` (when
    checking its *second* period) or an initial-heavy acronym like ``U.S`` is
    captured whole rather than just its last letter.
    """
    j = pos
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == '.'):
        j -= 1
    return text[j:pos]


def _is_abbreviation(token: str) -> bool:
    """Whether a trailing "." right after ``token`` should not end a sentence."""
    if not token:
        return False
    if len(token) == 1 and token.isalpha():
        return True  # a bare initial, e.g. the "J" in "J. K. Rowling"
    return token.lower().replace('.', '') in _ABBREVIATIONS


def _is_real_boundary(text: str, punct_start: int, punct: str, after: str) -> bool:
    """Whether a regex-matched candidate is an actual sentence boundary."""
    if punct == '.':
        if _is_abbreviation(_preceding_token(text, punct_start)):
            return False
    if not after:
        return True  # end of this spine item's text -- always a real boundary
    ch = after[0]
    return ch.isupper() or ch.isdigit() or ch in _SENTENCE_START_CHARS


def split_sentences(text: str, hard_breaks: Optional[Sequence[int]] = None) -> List[Tuple[int, int]]:
    """Split ``text`` into sentence spans, half-open ``[start, end)``.

    Deterministic and dependency-free: a regex scan for terminal punctuation
    with a hand-maintained abbreviation exception list, not a statistical
    tokenizer -- the same text must always produce the same spans, because a
    stored reading position is a sentence id and regenerating a book must not
    renumber its sentences.

    ``text`` is assumed already trimmed of leading/trailing whitespace, true
    of every ``extract_text_and_map`` spine-item slice (bs4's
    ``get_text(strip=True)`` strips each contributing string before joining,
    per ``src/utils/ebook_dom_map.py``'s own module docstring).

    A boundary candidate -- terminal punctuation, optional closing quote or
    bracket, then whitespace -- is accepted unless the punctuation is a lone
    "." immediately after an abbreviation or initial (:func:`_is_abbreviation`),
    or the next sentence would start with a lowercase letter (a trailing
    clause continuing past an ellipsis, or a rare stray terminator). The
    final span always runs to ``len(text)`` even when the text has no
    trailing punctuation, so content is never silently dropped, and a
    sentence never crosses the end of ``text`` -- callers scope this to one
    spine item's slice so a sentence never spans two XHTML documents.

    ``hard_breaks`` are local offsets where a sentence must end regardless of
    punctuation -- block-element starts from
    :func:`src.utils.ebook_dom_map.block_break_offsets` -- so an unpunctuated
    line (a heading, a credit, a caption) is its own sentence rather than
    merging into the next paragraph.

    :param text: one spine item's text, in ``extract_text_and_map``'s
        combined-text character space (a local, 0-based slice of it).
    :param hard_breaks: local offsets that always start a new sentence.
    :return: half-open ``(start, end)`` spans covering ``text``, in order.
    """
    if not text:
        return []

    spans: List[Tuple[int, int]] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(text):
        punct = match.group('punct')
        content_end = match.start() + len(punct) + len(match.group('quotes'))
        after = text[match.end():]

        if not _is_real_boundary(text, match.start(), punct, after):
            continue
        if content_end > start:
            spans.append((start, content_end))
        start = match.end()

    if start < len(text):
        spans.append((start, len(text)))

    if not hard_breaks:
        return spans

    breaks = sorted(set(hard_breaks))
    split_spans: List[Tuple[int, int]] = []
    for span_start, span_end in spans:
        cursor = span_start
        for brk in breaks[bisect_right(breaks, span_start):]:
            if brk >= span_end:
                break
            left_end = brk
            while left_end > cursor and text[left_end - 1].isspace():
                left_end -= 1
            if left_end > cursor:
                split_spans.append((cursor, left_end))
            cursor = brk
        if span_end > cursor:
            split_spans.append((cursor, span_end))
    return split_spans


def sentence_id_for(spine_index: int, local_index: int) -> str:
    """The stable id for the ``local_index``-th sentence of spine item
    ``spine_index`` (both as :func:`build_sentence_clips` uses them: 1-based
    ``spine_index`` matching ``extract_text_and_map``'s ``spine_map``,
    0-based ``local_index`` resetting per spine item).

    Follows Storyteller's ``-sN`` convention -- see this module's docstring
    for the confirmed source and why the chapter prefix here is
    ``c<spine_index>`` rather than Storyteller's manifest item id.
    """
    return f"c{spine_index}-s{local_index}"


@dataclass(frozen=True)
class SentenceClip:
    """One sentence's text span and interpolated audio clip.

    ``char_start``/``char_end`` are half-open, in the same character space as
    ``EbookParser.extract_text_and_map``'s combined text (and
    ``ebook_dom_map.SpineDomMap``). ``ts_start``/``ts_end`` are seconds into
    the book's audio; they are monotonic and non-overlapping within a segment.
    Segmented maps carry the segment identity and audio bounds so later EPUB
    assembly can preserve reordered narration blocks.
    """
    sentence_id: str
    spine_index: int
    href: str
    char_start: int
    char_end: int
    ts_start: float
    ts_end: float
    # Present for segmented maps so later assembly can distinguish a real
    # within-segment pause from a jump to an unrelated narration block.
    segment_key: Optional[int] = None
    segment_ts_start: Optional[float] = None
    segment_ts_end: Optional[float] = None
    segment_scoped: bool = False


@dataclass(frozen=True)
class SentenceClipResult:
    """The full per-book output of :func:`build_sentence_clips`.

    ``dropped_no_timestamp`` counts sentences excluded from ``clips`` because
    the alignment map returned no timestamp for one of their boundaries
    (``AlignmentService.get_time_for_char`` returning ``None`` -- only
    possible for a degenerate empty map), or -- for a segmented, out-of-order
    map -- because neither boundary landed inside any fitted segment at all
    (a genuinely unnarrated stretch; see :func:`build_sentence_clips`).
    ``clamped_count`` counts sentences
    whose interpolated start and/or end had to be pulled forward to keep the
    book monotonic and non-overlapping -- a diagnostic, not an error; see
    :func:`build_sentence_clips`.
    ``interpolated_count`` counts emitted clips with no aligned word inside
    their ``[char_start, char_end)`` span -- only meaningful for CTC maps
    (one entry per aligned word); ``None`` for any other alignment method.
    """
    abs_id: str
    clips: List[SentenceClip]
    dropped_no_timestamp: int
    clamped_count: int
    interpolated_count: Optional[int] = None


def _map_fits_epub(alignment_service: "AlignmentService", abs_id: str, combined_text_len: int) -> bool:
    """True only when ``abs_id``'s stored alignment map was fitted against
    text of exactly this length.

    Checks two independent fingerprints, preferring the more direct one:

    1. ``total_chars`` (``BookAlignment.total_chars``, read via
       ``AlignmentService.database_service.get_alignment_total_chars`` --
       both public attributes): every ``_publish_map`` call site in
       ``AlignmentService`` writes this as ``len(ebook_text)`` *at forge
       time*, deliberately and unconditionally -- it is a direct record of
       "this map was built against text of this length," not derived from
       where the anchors happened to land.
    2. ``AlignmentService.get_map_terminal_char`` (the map's own last
       anchor's char) -- the fallback ``SyncManager._get_alignment_epub_filename``
       uses, and the only signal available for a map predating the
       ``total_chars`` column (NULL) or stored with it as 0 (334 of 378 maps
       on the reference install).

    These two disagree more often than expected: measured live
    against this install, several CTC maps have a correctly-recorded
    ``total_chars`` exactly matching their current EPUB's length while their
    *last anchor* falls short of it by anywhere from a few hundred to
    hundreds of thousands of characters -- forced alignment (and lexical
    anchoring) does not always confidently anchor all the way to the final
    character of extracted text (back matter, acknowledgments, an unnarrated
    tail). Trusting the terminal char alone there would refuse a large
    fraction of genuinely-matching CTC maps for a reason that has nothing to
    do with a wrong EPUB. ``total_chars`` is authoritative when present
    because it is written directly, not inferred; the terminal-char check
    remains the fallback for the maps that predate it.

    ``total_chars`` is compared with a small absolute tolerance rather than for
    exact equality. Measured across every book on the reference install carrying
    a recorded ``total_chars`` (79 of them): 74 matched to the character, 4 were
    off by 1-15, one by 236, and none by more than that. The small deltas are
    extraction drift -- a re-stamped metadata field, a changed copyright line --
    on a book that is otherwise the same file. The condition this guard exists to
    catch is a *different edition*, which cannot differ by less than a sentence;
    the same install's real mismatch was 11,117 characters. So the tolerance is
    set below the length of a short sentence, which separates drift from a
    different book on principle rather than by fitting the sample.

    Returns False (refuse, never guess) when neither fingerprint is
    available or neither matches ``combined_text_len``.
    """
    try:
        total_chars = alignment_service.database_service.get_alignment_total_chars(abs_id)
    except Exception as e:
        logger.warning(
            "Could not read alignment total_chars for '%s': %s", abs_id, e, exc_info=True
        )
        total_chars = None
    if total_chars:
        return abs(int(total_chars) - int(combined_text_len)) <= _TOTAL_CHARS_DRIFT_TOLERANCE

    try:
        terminal = alignment_service.get_map_terminal_char(abs_id)
    except Exception as e:
        logger.warning(
            "Could not read alignment map terminal char for '%s': %s", abs_id, e, exc_info=True
        )
        return False
    if not terminal:
        return False
    return int(terminal) == int(combined_text_len)


def build_sentence_clips(
    parser: "EbookParser",
    filepath: Union[str, Path],
    alignment_service: "AlignmentService",
    abs_id: str,
) -> Optional[SentenceClipResult]:
    """Build the sentence + clip-time table for one book.

    Splits each spine item's text into sentences (:func:`split_sentences`,
    scoped so a sentence never crosses a spine item), then interpolates each
    sentence's ``(char_start, char_end)`` through ``abs_id``'s alignment map
    via ``AlignmentService.get_time_for_char`` -- already segment-aware for
    out-of-order narration (issue #426), so a reordered block is clamped to
    its own segment's edges rather than blended with its neighbour's.

    Refuses -- returns ``None`` -- rather than guess when the stored
    alignment map was not fitted against this exact EPUB (see
    :func:`_map_fits_epub`). A wrong map produces a read-along that drifts
    further the longer it plays, so this never emits output for one.

    Interpolated timestamps are clamped to be monotonically non-decreasing
    and non-overlapping *within whatever segment each sentence's own start
    belongs to* (a running floor at the previous clip's end, reset whenever
    the current sentence's segment differs from the previous one's -- see
    :func:`_segment_key_for_char`). A sentence whose two boundaries fall in
    two different out-of-order segments can still resolve to an end
    timestamp before its own start (each edge clamps independently to its
    *own* nearest segment edge); that is handled per-sentence, comparing only
    against that sentence's own start, never against a floor inherited from
    a different segment.

    **Finding 3 of the independent review of Phases 1-4 (fixed here):** an
    earlier version of this function kept a single running floor across the
    *whole book*, in spine/reading order. For a book with genuinely
    out-of-order narration (issue #426 segmented maps -- 15 of 324 books on
    the reference install), a chapter narrated *earlier* in the audio than a
    chapter that precedes it in reading order would have its legitimate
    (small) timestamps clamped up to the previous (reading-order) chapter's
    floor, collapsing it to a zero-length clip and making that chapter's
    audio unreachable. The floor is now scoped to a segment: crossing into a
    different segment (or into/out of unsegmented territory) starts a fresh
    floor at 0.0 rather than carrying forward a floor from audio the new
    segment has no ordering relationship with. For a book with no
    ``segments_json`` (the common case -- a single, in-order narration), this
    is unchanged from before: one segment spans the whole book, so the floor
    is still the single running one across all its sentences.

    This never silently emits a sentence with no timestamp at all -- one is
    only ever dropped, and counted, when the alignment map itself returns
    ``None`` for a boundary.

    A segmented map additionally drops -- rather than emits -- a sentence
    whose two boundaries land in a segment *gap*: chars no fitted segment
    covers at all (front/back matter within an otherwise narrated book, or a
    chapter that never fit). ``get_time_for_char`` has no notion of "no
    coverage" and answers a gap with the char-nearest segment edge's
    timestamp regardless, so trusting that value there would silently
    replay a neighbouring segment's audio over text that was never narrated.
    A sentence that crosses a segment boundary (one edge inside a segment,
    the other outside it or inside a different one) is not dropped -- it is
    clamped into the segment its own start belongs to, so it never reaches
    into a different segment's unrelated timestamps.

    :param parser: the ``EbookParser`` to source the book's spine text from.
    :param filepath: the EPUB path, exactly as ``extract_text_and_map`` accepts
        (an existing path, or a bare filename ``resolve_book_path`` can find).
    :param alignment_service: source of the book's stored alignment map.
    :param abs_id: the book's ABS id (the alignment map's primary key).
    :return: the per-book result, or ``None`` if the fitted-EPUB guard refused.
    """
    combined_text, spine_map = parser.extract_text_and_map(filepath)

    if not _map_fits_epub(alignment_service, abs_id, len(combined_text)):
        logger.warning(
            "🚫 Refusing to generate read-along sentences for '%s': stored "
            "alignment map was not fitted against '%s' (fitted-EPUB guard)",
            abs_id, filepath,
        )
        return None

    segments = alignment_service._get_segments(abs_id)
    # Anything but a real list (a test double's auto-attribute) means "no
    # per-word offsets", the same as a non-CTC map.
    word_offsets = alignment_service.aligned_word_offsets(abs_id)
    if not isinstance(word_offsets, list):
        word_offsets = None
    interpolated = 0

    clips: List[SentenceClip] = []
    dropped = 0
    clamped = 0
    floor_ts = 0.0
    floor_segment_key: object = _UNSET_SEGMENT

    for entry in spine_map:
        item_text = combined_text[entry["start"]:entry["end"]]
        block_breaks = block_break_offsets(entry["content"], item_text) if item_text else None
        if item_text and block_breaks is None:
            logger.warning(
                "'%s' spine item %s: block boundaries could not be recovered, "
                "splitting sentences on punctuation only",
                abs_id, entry["spine_index"],
            )
        for local_index, (local_start, local_end) in enumerate(split_sentences(item_text, block_breaks)):
            char_start = entry["start"] + local_start
            char_end = entry["start"] + local_end
            sentence_id = sentence_id_for(entry["spine_index"], local_index)

            raw_start = alignment_service.get_time_for_char(abs_id, char_start)
            raw_end = alignment_service.get_time_for_char(abs_id, char_end)
            if raw_start is None or raw_end is None:
                dropped += 1
                logger.warning(
                    "'%s' sentence %s (chars %d-%d): alignment map returned no "
                    "timestamp for a boundary, dropping",
                    abs_id, sentence_id, char_start, char_end,
                )
                continue

            segment = None
            if segments:
                segment_key = _segment_key_for_char(segments, char_start, char_end)
                segment = _segment_for_char(segments, char_start)
                if segment is None:
                    segment = _segment_for_char(segments, char_end)
                if segment is None:
                    # Neither boundary lands inside any fitted segment: a
                    # genuinely unnarrated stretch (front/back matter, or an
                    # out-of-order book's chapter that never fit -- the
                    # independent review's own repro: reading order A,
                    # unnarrated, B, C with fitted ranges A=0-2s, B=4-6s,
                    # C=2-4s). `AlignmentService.get_time_for_char` still
                    # answers with *some* timestamp here -- it has no notion
                    # of "no coverage" and clamps to whichever segment edge
                    # is char-nearest -- so trusting it would silently
                    # duplicate that neighbour's audio onto text that was
                    # never narrated (there the unnarrated span was assigned
                    # C's own 2-4s). Drop it exactly like a missing-timestamp
                    # boundary: from this book's real audio's perspective, it
                    # is one.
                    dropped += 1
                    logger.warning(
                        "'%s' sentence %s (chars %d-%d): falls outside every "
                        "fitted alignment segment, dropping rather than "
                        "reusing a neighbouring segment's audio",
                        abs_id, sentence_id, char_start, char_end,
                    )
                    continue
                # ``char_end`` is half-open. At an exact segment boundary
                # the alignment lookup may land in the next segment; a
                # sentence belongs to the segment containing its start.
                segment_start = float(segment["ts_start"])
                segment_end = float(segment["ts_end"])
                raw_start = min(max(float(raw_start), segment_start), segment_end)
                raw_end = min(max(float(raw_end), segment_start), segment_end)
                if segment_key != floor_segment_key:
                    # A genuine narration-order jump to a different segment
                    # (or the very first sentence): the previous segment's
                    # ending timestamp has no ordering relationship with this
                    # one, so start a fresh floor instead of forcing this
                    # sentence to not go "backward" relative to audio it
                    # doesn't share a segment with (Finding 3).
                    floor_ts = 0.0
                    floor_segment_key = segment_key

            ts_start = max(float(raw_start), floor_ts)
            ts_end = max(float(raw_end), ts_start)
            if ts_start > float(raw_start) or ts_end > float(raw_end):
                clamped += 1

            clips.append(SentenceClip(
                sentence_id=sentence_id,
                spine_index=entry["spine_index"],
                href=entry["href"],
                char_start=char_start,
                char_end=char_end,
                ts_start=ts_start,
                ts_end=ts_end,
                segment_key=segment_key if segments else None,
                segment_ts_start=float(segment["ts_start"]) if segment is not None else None,
                segment_ts_end=float(segment["ts_end"]) if segment is not None else None,
                segment_scoped=bool(segments),
            ))
            floor_ts = ts_end
            if word_offsets is not None:
                first = bisect_left(word_offsets, char_start)
                if first >= len(word_offsets) or word_offsets[first] >= char_end:
                    interpolated += 1

    return SentenceClipResult(
        abs_id=abs_id,
        clips=clips,
        dropped_no_timestamp=dropped,
        clamped_count=clamped,
        interpolated_count=interpolated if word_offsets is not None else None,
    )
