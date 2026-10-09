"""Unit tests for sentence segmentation and clip-time interpolation (read-along
EPUB 3 generation).

Builds small inline EPUB fixtures with zipfile (same pattern as
test_ebook_dom_map.py / test_ebook_utils_spine_manifest_gap.py). Alignment maps
are supplied via a minimal fake matching only the AlignmentService surface
src.services.readalong_segments depends on (get_map_terminal_char,
get_time_for_char, and the public database_service.get_alignment_total_chars
passthrough) -- AlignmentService's own interpolation is already tested
elsewhere; these tests exercise how this module consumes it.
"""
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.services.readalong_segments import (
    build_sentence_clips,
    sentence_id_for,
    split_sentences,
)
from src.utils.ebook_dom_map import block_break_offsets
from src.utils.ebook_utils import EbookParser

_CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _parser(tmp: Path) -> EbookParser:
    books = tmp / "books"
    cache = tmp / "cache"
    books.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    return EbookParser(books_dir=str(books), epub_cache_dir=str(cache))


def _opf(manifest_ids: List[str], spine_idrefs: List[str]) -> str:
    manifest = "".join(
        f'<item id="{iid}" href="{iid}.xhtml" media-type="application/xhtml+xml"/>'
        for iid in manifest_ids
    )
    spine = "".join(f'<itemref idref="{iid}"/>' for iid in spine_idrefs)
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="2.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
        '<dc:identifier id="id">x</dc:identifier></metadata>'
        f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>'
    )


def _write_epub(path: Path, items: Dict[str, bytes]) -> None:
    """``items``: {item_id: xhtml_bytes}. Spine order is dict order."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _opf(list(items.keys()), list(items.keys())))
        for item_id, content in items.items():
            z.writestr(f"OEBPS/{item_id}.xhtml", content)


class _FakeAlignmentService:
    """Minimal test double for the AlignmentService surface
    build_sentence_clips depends on: get_map_terminal_char, get_time_for_char,
    and the public database_service.get_alignment_total_chars passthrough
    the fitted-EPUB guard prefers (see _map_fits_epub)."""

    class _FakeDatabaseService:
        def __init__(self, total_chars: Optional[int]):
            self._total_chars = total_chars

        def get_alignment_total_chars(self, abs_id: str) -> Optional[int]:
            return self._total_chars

    def __init__(
        self,
        terminal_char: Optional[int],
        time_for_char: Callable[[int], Optional[float]],
        total_chars: Optional[int] = None,
        segments: Optional[List[Dict]] = None,
        word_offsets: Optional[List[int]] = None,
    ):
        self._terminal_char = terminal_char
        self._time_for_char = time_for_char
        self.database_service = self._FakeDatabaseService(total_chars)
        self._segments = segments
        self._word_offsets = word_offsets

    def get_map_terminal_char(self, abs_id: str) -> Optional[int]:
        return self._terminal_char

    def aligned_word_offsets(self, abs_id: str) -> Optional[list]:
        return self._word_offsets

    def get_time_for_char(self, abs_id: str, char_offset: int) -> Optional[float]:
        return self._time_for_char(char_offset)

    def _get_segments(self, abs_id: str) -> Optional[List[Dict]]:
        """Same "friend" access pattern this repo's own AlignmentService
        tests use directly on the real class (see e.g. test_segmented_map.py) --
        None means an unsegmented (single, in-order narration) map, matching
        the real AlignmentService._get_segments contract."""
        return self._segments


def _linear_interpolator(points: List[Dict]) -> Callable[[int], Optional[float]]:
    """A bare-bones reimplementation of flat linear char->ts interpolation
    (no segments), for tests -- AlignmentService's real interpolation is
    tested in its own suite; this only needs to behave like it for these
    call-site tests (clamped ends, linear between two anchors)."""
    chars = [p["char"] for p in points]
    tss = [p["ts"] for p in points]

    def interpolate(char_offset: int) -> float:
        if char_offset <= chars[0]:
            return tss[0]
        if char_offset >= chars[-1]:
            return tss[-1]
        for i in range(len(chars) - 1):
            if chars[i] <= char_offset <= chars[i + 1]:
                span = chars[i + 1] - chars[i]
                if span == 0:
                    return tss[i]
                frac = (char_offset - chars[i]) / span
                return tss[i] + frac * (tss[i + 1] - tss[i])
        return tss[-1]

    return interpolate


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------

def test_split_sentences_basic_two_sentences():
    text = "Alpha bravo charlie. Delta echo foxtrot."
    spans = split_sentences(text)
    assert [text[s:e] for s, e in spans] == ["Alpha bravo charlie.", "Delta echo foxtrot."]
    # Spans are contiguous modulo the single separating space, and cover the
    # whole string with no gaps except that space.
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)


def test_split_sentences_abbreviations_not_split():
    text = "Mr. Smith met Dr. Jones, e.g. at noon. They left."
    spans = split_sentences(text)
    sentences = [text[s:e] for s, e in spans]
    assert sentences == ["Mr. Smith met Dr. Jones, e.g. at noon.", "They left."]


def test_split_sentences_ellipsis_followed_by_new_sentence_splits():
    text = "He paused... Where did he go?"
    spans = split_sentences(text)
    sentences = [text[s:e] for s, e in spans]
    assert sentences == ["He paused...", "Where did he go?"]


def test_split_sentences_ellipsis_trailing_into_lowercase_does_not_split():
    text = "Well... obviously that was a mistake."
    spans = split_sentences(text)
    sentences = [text[s:e] for s, e in spans]
    assert sentences == ["Well... obviously that was a mistake."]


def test_split_sentences_quote_ends_sentence_after_closing_mark():
    text = 'She said, "Stop." He ran.'
    spans = split_sentences(text)
    sentences = [text[s:e] for s, e in spans]
    assert sentences == ['She said, "Stop."', "He ran."]


def test_split_sentences_no_trailing_punctuation_still_emits_full_span():
    text = "Chapter One"
    spans = split_sentences(text)
    assert spans == [(0, len(text))]


def test_split_sentences_empty_text():
    assert split_sentences("") == []


def test_split_sentences_hard_breaks_split_unpunctuated_lines():
    text = "Cover design by Grim Poppy Design Edited by Danielle Sundby"
    brk = text.index("Edited")
    spans = split_sentences(text, [brk])
    assert [text[s:e] for s, e in spans] == ["Cover design by Grim Poppy Design", "Edited by Danielle Sundby"]


def test_split_sentences_hard_break_at_punctuation_boundary_adds_nothing():
    text = "Alpha bravo. Charlie delta."
    assert split_sentences(text, [text.index("Charlie")]) == split_sentences(text)


def test_split_sentences_hard_break_inside_punctuated_sentence():
    """A sentence split across two paragraphs without terminal punctuation
    (a heading followed by body text) becomes two targets, one per block."""
    text = "Chapter One It was a dark night. The end."
    spans = split_sentences(text, [text.index("It was")])
    assert [text[s:e] for s, e in spans] == ["Chapter One", "It was a dark night.", "The end."]


# ---------------------------------------------------------------------------
# block_break_offsets
# ---------------------------------------------------------------------------

def test_block_break_offsets_marks_each_new_block_but_not_inline_runs():
    content = b"<html><body><h1>Chapter One</h1><p>It was <em>very</em> dark</p><p>Next</p></body></html>"
    text = "Chapter One It was very dark Next"
    assert block_break_offsets(content, text) == [text.index("It was"), text.index("Next")]


def test_block_break_offsets_refuses_drifted_text():
    content = b"<html><body><p>Alpha</p><p>Bravo</p></body></html>"
    assert block_break_offsets(content, "Alpha Charlie") is None


# ---------------------------------------------------------------------------
# sentence_id_for
# ---------------------------------------------------------------------------

def test_sentence_id_scheme():
    assert sentence_id_for(1, 0) == "c1-s0"
    assert sentence_id_for(3, 5) == "c3-s5"


# ---------------------------------------------------------------------------
# build_sentence_clips
# ---------------------------------------------------------------------------

def test_sentence_never_crosses_spine_item_boundary():
    """Each spine item's own sentence(s) start fresh -- a sentence never
    spans two XHTML documents, and the last sentence of an item ends exactly
    at that item's own text boundary."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "two_items.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence here. Second one too.</p></body></html>",
            "ch2": b"<html><body><p>Third sentence starts fresh.</p></body></html>",
        })

        combined_text, spine_map = parser.extract_text_and_map(str(epub_path))
        fake = _FakeAlignmentService(
            terminal_char=len(combined_text),
            time_for_char=_linear_interpolator(
                [{"char": 0, "ts": 0.0}, {"char": len(combined_text), "ts": 100.0}]
            ),
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None

        item1, item2 = spine_map[0], spine_map[1]
        item1_clips = [c for c in result.clips if c.spine_index == item1["spine_index"]]
        item2_clips = [c for c in result.clips if c.spine_index == item2["spine_index"]]

        assert len(item1_clips) == 2
        assert len(item2_clips) == 1
        # No clip's char range crosses its own spine item's [start, end).
        for clip in item1_clips:
            assert item1["start"] <= clip.char_start and clip.char_end <= item1["end"]
        for clip in item2_clips:
            assert item2["start"] <= clip.char_start and clip.char_end <= item2["end"]
        # The last sentence of item 1 ends exactly at item 1's own boundary.
        assert item1_clips[-1].char_end == item1["end"]
        # The first sentence of item 2 starts exactly at item 2's own boundary.
        assert item2_clips[0].char_start == item2["start"]
        # ids reset per spine item.
        assert [c.sentence_id for c in item1_clips] == ["c1-s0", "c1-s1"]
        assert [c.sentence_id for c in item2_clips] == ["c2-s0"]


def test_deterministic_ids_across_two_runs():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "det.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo. Charlie delta. Echo foxtrot.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        fake = _FakeAlignmentService(
            terminal_char=len(combined_text),
            time_for_char=_linear_interpolator(
                [{"char": 0, "ts": 0.0}, {"char": len(combined_text), "ts": 10.0}]
            ),
        )

        first = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        second = build_sentence_clips(parser, str(epub_path), fake, "abs1")

        assert first is not None and second is not None
        as_tuples = lambda r: [  # noqa: E731
            (c.sentence_id, c.char_start, c.char_end, c.ts_start, c.ts_end) for c in r.clips
        ]
        assert as_tuples(first) == as_tuples(second)
        assert len(first.clips) >= 3


def test_interpolation_clamps_at_and_beyond_map_endpoints():
    """A sentence starting before the map's first anchor clamps to the first
    anchor's timestamp; one ending at/after the last anchor clamps to the
    last anchor's timestamp (the guard requires the last anchor to equal
    len(combined_text) exactly, so this is the book's very first and last
    sentences)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "sparse.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo charlie. Delta echo foxtrot.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        total = len(combined_text)
        # Anchors only cover the middle of the book -- chars 5..total-5.
        fake = _FakeAlignmentService(
            terminal_char=total,
            time_for_char=_linear_interpolator(
                [{"char": 5, "ts": 2.0}, {"char": total - 5, "ts": 50.0}]
            ),
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None
        assert result.clips[0].char_start == 0
        assert result.clips[0].ts_start == 2.0  # clamped to the first anchor
        assert result.clips[-1].char_end == total
        assert result.clips[-1].ts_end == 50.0  # clamped to the last anchor


def test_fitted_epub_guard_refuses_mismatched_map():
    """A map fitted against a different EPUB (different text length) must be
    refused outright, never guessed at."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "mismatch.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Some real book text goes here.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        wrong_terminal = len(combined_text) + 37  # simulates a different edition's length
        fake = _FakeAlignmentService(
            terminal_char=wrong_terminal,
            time_for_char=_linear_interpolator(
                [{"char": 0, "ts": 0.0}, {"char": wrong_terminal, "ts": 10.0}]
            ),
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is None


def test_fitted_epub_guard_accepts_matching_map():
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "match.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Some real book text goes here.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        fake = _FakeAlignmentService(
            terminal_char=len(combined_text),
            time_for_char=_linear_interpolator(
                [{"char": 0, "ts": 0.0}, {"char": len(combined_text), "ts": 10.0}]
            ),
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None
        assert result.dropped_no_timestamp == 0


def test_fitted_epub_guard_prefers_total_chars_over_a_short_terminal_anchor():
    """Live-verified real-world case (State of Fear, Summer of Night on the
    reference install): a CTC map's own last anchor can fall well short of
    the ebook's actual length -- forced alignment doesn't always confidently
    anchor all the way to the final character (an unnarrated tail, back
    matter) -- even though the map is genuinely fitted to the current EPUB
    and total_chars (recorded directly at forge time) says so. The guard
    must not refuse a correctly-matched map just because its terminal anchor
    is short."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "short_tail.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Some real book text goes here.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        total = len(combined_text)
        fake = _FakeAlignmentService(
            terminal_char=total - 10,  # anchors stop 10 chars short of the real end
            time_for_char=_linear_interpolator(
                [{"char": 0, "ts": 0.0}, {"char": total - 10, "ts": 10.0}]
            ),
            total_chars=total,  # but total_chars, recorded at forge time, is correct
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None


def test_fitted_epub_guard_refuses_when_total_chars_itself_mismatches():
    """total_chars is trusted when present, but still enforced -- a map whose
    recorded total_chars disagrees with the current EPUB is refused even if
    checking it were skipped it might otherwise look plausible."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "wrong_total_chars.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Some real book text goes here.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        total = len(combined_text)
        fake = _FakeAlignmentService(
            terminal_char=total,  # terminal happens to match...
            time_for_char=_linear_interpolator([{"char": 0, "ts": 0.0}, {"char": total, "ts": 10.0}]),
            total_chars=total + 500,  # ...but the recorded total_chars does not
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is None


def test_fitted_epub_guard_tolerates_small_extraction_drift():
    """A handful of characters is a re-stamped metadata field, not a different
    book. Measured on the reference install: of 79 maps carrying a recorded
    total_chars, 74 matched exactly, 4 were off by 1-15, and the one real
    wrong-edition case was off by 11,117. The tolerance sits below the length
    of a short sentence so drift passes and a different edition cannot."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "drifted.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence. Second sentence. Third one here.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        total = len(combined_text)
        interp = _linear_interpolator([{"char": 0, "ts": 0.0}, {"char": total, "ts": 10.0}])

        # 12 characters of drift: accepted.
        drifted = _FakeAlignmentService(
            terminal_char=total, time_for_char=interp, total_chars=total - 12,
        )
        assert build_sentence_clips(parser, str(epub_path), drifted, "abs1") is not None

        # A sentence's worth of difference: still refused.
        edition = _FakeAlignmentService(
            terminal_char=total, time_for_char=interp, total_chars=total - 240,
        )
        assert build_sentence_clips(parser, str(epub_path), edition, "abs1") is None


def test_out_of_order_timestamps_are_clamped_monotonic_and_non_overlapping():
    """A backward jump in the raw per-boundary timestamps with NO segment
    structure behind it (``_get_segments`` returns ``None`` -- an
    unsegmented/legacy map, or noise within what is otherwise one single
    narration) must still never surface as a clip that ends before it
    starts, or overlaps its predecessor: without segment boundaries to
    explain a backward jump, clamping to the running floor is the only safe
    interpretation.

    This is deliberately narrower than it looks: it is NOT a claim that any
    backward jump should always be clamped to the whole-book floor -- see
    ``test_out_of_order_segments_preserve_reordered_narration_timestamps``
    directly below for the case (real ``segments_json``, issue #426) where
    clamping across the jump would be Finding 3's corruption instead. This
    test's own fake supplies no segments, so it never exercises that branch."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "reordered.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence. Second sentence. Third sentence.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        total = len(combined_text)

        # Craft a raw lookup that jumps backwards for the middle sentence,
        # simulating two boundaries landing in different out-of-order segments.
        def raw(char_offset: int) -> float:
            if char_offset < total // 3:
                return 10.0 + char_offset * 0.01
            if char_offset < 2 * total // 3:
                return 1.0  # backward jump
            return 20.0 + char_offset * 0.01

        fake = _FakeAlignmentService(terminal_char=total, time_for_char=raw)

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None
        assert len(result.clips) >= 3
        assert result.clamped_count >= 1

        prev_end = 0.0
        for clip in result.clips:
            assert clip.ts_start >= prev_end  # monotonic, non-overlapping
            assert clip.ts_end >= clip.ts_start  # no negative duration
            prev_end = clip.ts_end


def test_out_of_order_segments_preserve_reordered_narration_timestamps():
    """Finding 3 (independent review of Phases 1-4, fixed): a book with
    genuinely out-of-order narration (issue #426 segmented maps -- 15 of 324
    books on the reference install) must not have a later-reading-order-but-
    earlier-narrated segment's legitimate, small timestamps clamped up to an
    earlier-reading-order segment's floor. Before the fix, a single running
    floor across the whole book collapsed the second (earlier-narrated)
    segment's sentence into a zero-length clip -- the reviewer's own
    reproduction: correct ranges [10, 20] and [0, 9] became [10, 20] and
    [20, 20]. Fixed by scoping the monotonic floor to a segment and resetting
    it on every segment transition."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "reordered_segments.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence here. Second sentence follows.</p></body></html>",
        })
        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        spans = split_sentences(combined_text)
        assert len(spans) == 2
        s0_start, s0_end = spans[0]
        s1_start, s1_end = spans[1]

        # Segment A (reading-order first, narrated LATER -- ts 10..20) covers
        # sentence 0; Segment B (reading-order second, narrated EARLIER --
        # ts 0..9) covers sentence 1. This is exactly the out-of-order shape
        # issue #426's segmented maps exist to represent.
        segments = [
            {"char_start": s0_start, "char_end": s0_end, "ts_start": 10.0, "ts_end": 20.0},
            {"char_start": s1_start, "char_end": s1_end, "ts_start": 0.0, "ts_end": 9.0},
        ]

        def raw(char_offset: int) -> float:
            if s0_start <= char_offset <= s0_end:
                frac = (char_offset - s0_start) / max(1, s0_end - s0_start)
                return 10.0 + frac * 10.0
            frac = (char_offset - s1_start) / max(1, s1_end - s1_start)
            return 0.0 + frac * 9.0

        fake = _FakeAlignmentService(
            terminal_char=len(combined_text), time_for_char=raw, segments=segments,
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None
        assert len(result.clips) == 2

        clip0, clip1 = result.clips
        assert clip0.ts_start == 10.0
        assert clip0.ts_end == 20.0
        # The bug collapsed this to (20.0, 20.0) -- a zero-length, unreachable
        # clip. Fixed: segment B's own legitimate, earlier timestamps survive.
        assert clip1.ts_start == 0.0
        assert clip1.ts_end == 9.0
        assert clip1.ts_end > clip1.ts_start  # not a zero-length clip


def test_dropped_sentence_when_alignment_returns_no_timestamp():
    """A boundary the alignment map can't place is dropped and counted, never
    silently emitted with a fabricated timestamp."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "hole.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>First sentence. Second sentence.</p></body></html>",
        })

        combined_text, _ = parser.extract_text_and_map(str(epub_path))
        total = len(combined_text)

        # Find the char_end of the first sentence by splitting the item text
        # the same way build_sentence_clips will.
        spans = split_sentences(combined_text)
        hole_char = spans[0][1]  # first sentence's char_end

        def raw(char_offset: int) -> Optional[float]:
            if char_offset == hole_char:
                return None
            return 1.0 + char_offset * 0.01

        fake = _FakeAlignmentService(terminal_char=total, time_for_char=raw)

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None
        assert result.dropped_no_timestamp == 1
        assert "c1-s0" not in [c.sentence_id for c in result.clips]
        assert "c1-s1" in [c.sentence_id for c in result.clips]


def test_sentence_outside_every_segment_is_dropped_not_reassigned_neighbour_audio():
    """Independent review's exact reproduction (P1): a sentence whose chars
    fall entirely outside every fitted segment must be dropped, not handed a
    neighbouring segment's audio.

    Reading order is A, an unnarrated stretch, B, C. Fitted ranges: A =
    0-2s, B = 4-6s, C = 2-4s (B and C are narrated out of reading order --
    the issue #426 shape). The unnarrated stretch has no segment of its own
    at all. Before the fix, `AlignmentService.get_time_for_char` still
    answers a char with no covering segment (it clamps to whichever segment
    edge is char-nearest -- see `_nearest_segment_edge_ts`), and nothing in
    this module rejected that answer, so the unnarrated stretch was
    assigned C's own 2-4s range verbatim -- a real 6-second audio file
    (0-2 + 4-6 + 2-4, no overlaps) exporting 8 seconds of clips (0-2 + 4-6 +
    2-4 + a duplicate 2-4)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "gap.epub"
        _write_epub(epub_path, {
            "cha": b"<html><body><p>Alpha bravo charlie</p></body></html>",
            "chb": b"<html><body><p>Delta echo foxtrot</p></body></html>",  # unnarrated
            "chc": b"<html><body><p>Golf hotel india</p></body></html>",
            "chd": b"<html><body><p>Juliet kilo lima</p></body></html>",
        })

        combined_text, spine_map = parser.extract_text_and_map(str(epub_path))
        a_entry, gap_entry, b_entry, c_entry = spine_map

        segments = [
            {"char_start": a_entry["start"], "char_end": a_entry["end"], "ts_start": 0.0, "ts_end": 2.0},
            {"char_start": b_entry["start"], "char_end": b_entry["end"], "ts_start": 4.0, "ts_end": 6.0},
            {"char_start": c_entry["start"], "char_end": c_entry["end"], "ts_start": 2.0, "ts_end": 4.0},
        ]

        def raw(char_offset: int) -> float:
            for entry, ts_start, ts_end in (
                (a_entry, 0.0, 2.0), (b_entry, 4.0, 6.0), (c_entry, 2.0, 4.0),
            ):
                if entry["start"] <= char_offset <= entry["end"]:
                    span = max(1, entry["end"] - entry["start"])
                    frac = (char_offset - entry["start"]) / span
                    return ts_start + frac * (ts_end - ts_start)
            # The unnarrated stretch: simulate get_time_for_char's own
            # nearest-segment-edge fallback landing squarely on C's range,
            # exactly the reviewer's reproduction ("assigned 2-4s").
            if char_offset == gap_entry["start"]:
                return 2.0
            if char_offset == gap_entry["end"]:
                return 4.0
            raise AssertionError(f"unexpected char_offset {char_offset}")

        fake = _FakeAlignmentService(
            terminal_char=len(combined_text), time_for_char=raw, segments=segments,
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None

        clip_ids = [c.sentence_id for c in result.clips]
        assert "c2-s0" not in clip_ids  # the unnarrated sentence is dropped
        assert result.dropped_no_timestamp == 1
        assert set(clip_ids) == {"c1-s0", "c3-s0", "c4-s0"}

        by_id = {c.sentence_id: c for c in result.clips}
        assert (by_id["c1-s0"].ts_start, by_id["c1-s0"].ts_end) == (0.0, 2.0)
        assert (by_id["c3-s0"].ts_start, by_id["c3-s0"].ts_end) == (4.0, 6.0)
        assert (by_id["c4-s0"].ts_start, by_id["c4-s0"].ts_end) == (2.0, 4.0)

        # The real audio is 6 seconds (0-2, 4-6, 2-4, no overlap). Before the
        # fix this summed to 8 seconds -- the dropped sentence's duplicate of
        # C's own 2-4s range.
        real_audio_duration = 6.0
        total_clip_time = sum(c.ts_end - c.ts_start for c in result.clips)
        assert total_clip_time <= real_audio_duration, (
            f"summed clip time {total_clip_time}s exceeds the real audio's "
            f"{real_audio_duration}s -- the unnarrated sentence duplicated a "
            f"neighbouring segment"
        )
        # Specifically: the unnarrated sentence must not have been given
        # any part of C's own [2.0, 4.0) range -- only c4-s0 may claim it.
        for clip in result.clips:
            if clip.sentence_id == "c4-s0":
                continue
            overlap_start = max(clip.ts_start, 2.0)
            overlap_end = min(clip.ts_end, 4.0)
            assert overlap_end <= overlap_start, (
                f"{clip.sentence_id} [{clip.ts_start}, {clip.ts_end}) overlaps "
                f"c4-s0's real audio [2.0, 4.0)"
            )


def test_sentence_spanning_a_segment_boundary_clamps_to_its_own_start_segment():
    """A single sentence whose char range straddles two different fitted
    segments must not be handed the far segment's unrelated timestamps --
    it clamps to the segment its own start belongs to, per
    `build_sentence_clips`'s own documented precedence (the segment
    containing the start wins over one only reachable via the end)."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        parser = _parser(tmp)
        epub_path = tmp / "books" / "crossing.epub"
        _write_epub(epub_path, {
            "ch1": b"<html><body><p>Alpha bravo charlie delta echo</p></body></html>",
        })

        combined_text, spine_map = parser.extract_text_and_map(str(epub_path))
        entry = spine_map[0]
        item_text = combined_text[entry["start"]:entry["end"]]
        assert len(split_sentences(item_text)) == 1  # one sentence spans the whole item
        mid = entry["start"] + len(item_text) // 2

        # Segment X (reading-order first) = 0-2s; segment Y (reading-order
        # second, unrelated to this sentence's start) = 5-7s.
        segments = [
            {"char_start": entry["start"], "char_end": mid, "ts_start": 0.0, "ts_end": 2.0},
            {"char_start": mid, "char_end": entry["end"], "ts_start": 5.0, "ts_end": 7.0},
        ]

        def raw(char_offset: int) -> float:
            if char_offset <= mid:
                frac = (char_offset - entry["start"]) / max(1, mid - entry["start"])
                return 0.0 + frac * 2.0
            frac = (char_offset - mid) / max(1, entry["end"] - mid)
            return 5.0 + frac * 2.0  # a real value from segment Y's own range

        fake = _FakeAlignmentService(
            terminal_char=len(combined_text), time_for_char=raw, segments=segments,
        )

        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result is not None
        assert result.dropped_no_timestamp == 0  # it has real narration -- not dropped
        assert len(result.clips) == 1

        clip = result.clips[0]
        # Confined to segment X's own [0.0, 2.0) -- never reaches segment Y's
        # real [5.0, 7.0) range, even though the sentence's own end char
        # lands inside Y and Y's raw lookup (7.0) would say otherwise.
        assert clip.ts_start == 0.0
        assert clip.ts_end == 2.0
        assert clip.ts_end >= clip.ts_start


# ---------------------------------------------------------------------------
# Interpolation count (per-build quality report)
# ---------------------------------------------------------------------------

def _two_sentence_epub(tmp: Path):
    parser = _parser(tmp)
    epub_path = tmp / "books" / "book.epub"
    _write_epub(epub_path, {"ch1": b"<html><body><p>First sentence here. Second sentence here.</p></body></html>"})
    combined_text, _ = parser.extract_text_and_map(str(epub_path))
    return parser, epub_path, combined_text


def test_sentence_with_no_anchor_inside_is_counted_interpolated_for_a_ctc_map():
    with tempfile.TemporaryDirectory() as tmp_str:
        parser, epub_path, text = _two_sentence_epub(Path(tmp_str))
        second = text.index("Second")
        fake = _FakeAlignmentService(
            terminal_char=len(text), time_for_char=lambda c: c / 10.0,
            total_chars=len(text), word_offsets=[0, 6, second - 1],
        )
        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert [c.char_start for c in result.clips] == [0, second]
        # the first sentence holds offsets 0 and 6; the second holds none
        assert result.interpolated_count == 1


def test_interpolated_count_is_none_for_a_non_ctc_map():
    with tempfile.TemporaryDirectory() as tmp_str:
        parser, epub_path, text = _two_sentence_epub(Path(tmp_str))
        fake = _FakeAlignmentService(
            terminal_char=len(text), time_for_char=lambda c: c / 10.0,
            total_chars=len(text), word_offsets=None,
        )
        result = build_sentence_clips(parser, str(epub_path), fake, "abs1")
        assert result.interpolated_count is None


def test_aligned_word_offsets_returns_sorted_offsets_for_ctc_and_none_otherwise():
    import json
    import shutil
    from src.db.database_service import DatabaseService
    from src.db.models import BookAlignment
    from src.services.alignment_service import AlignmentService
    from src.utils.polisher import Polisher

    temp_dir = tempfile.mkdtemp()
    db = DatabaseService(str(Path(temp_dir) / "offsets.db"))
    try:
        points = [{"char": 30, "ts": 3.0}, {"char": 0, "ts": 0.0}, {"char": 12, "ts": 1.2}]
        with db.get_session() as session:
            for abs_id, method in (("ctc-book", "ctc"), ("lex-book", "lexical")):
                session.add(BookAlignment(
                    abs_id=abs_id, alignment_map_json=json.dumps(points),
                    align_method=method, total_chars=40,
                ))
        service = AlignmentService(db, Polisher())
        assert service.aligned_word_offsets("ctc-book") == [0, 12, 30]
        assert service.aligned_word_offsets("lex-book") is None
        assert service.aligned_word_offsets("missing") is None
    finally:
        db.db_manager.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
