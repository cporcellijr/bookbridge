"""Interior ebook-only text must not displace narrated CTC words (#426)."""

from unittest.mock import patch

import pytest

from src.db.database_service import DatabaseService
from src.services.alignment_service import AlignmentService
from src.utils.forced_aligner import ForcedAligner
from src.utils.polisher import Polisher


def lexical_gap(gap=2000, seconds=1.0):
    text = "word " * 800 + "note " * (gap // 5) + "word " * 800
    anchors = [{"char": c, "ts": c / 40, "t_idx": c // 5}
               for c in range(0, 4001, 200)]
    anchors += [{"char": c + gap, "ts": c / 40 + seconds, "t_idx": c // 5 + 1}
                for c in range(4000, 8000, 200)]
    anchors.append({"char": len(text), "ts": 200 + seconds})
    return text, anchors, (4059, 4000 + gap)


# Anchors are 12-gram starts, so a real gap's bracketing pair always spans ~12
# narrated words (several seconds) — detection must key on density, not an absolute
# Δts cap, or it silently misses gaps in normally-paced books.
@pytest.mark.parametrize("seconds", [1.0, 4.0, 8.0])
def test_detect_interior_gap_preserves_both_matched_words(seconds):
    text, anchors, span = lexical_gap(seconds=seconds)
    assert AlignmentService._detect_unnarrated_spans(anchors, text) == [span]
    assert text[span[0] - 4:span[0]] == "note"
    assert text[span[1]:span[1] + 4] == "word"
    assert AlignmentService._detect_unnarrated_spans(list(reversed(anchors)), text) == [span]


# (2000, 50) is genuinely slow narration (~1x density); the short/tiny gaps fall
# under the min-span floor. None is a density anomaly, so none is excluded.
@pytest.mark.parametrize("gap,seconds", [(0, 1), (1000, 1), (2000, 50)])
def test_uniform_short_or_slow_gap_is_not_excluded(gap, seconds):
    text, anchors, _ = lexical_gap(gap, seconds)
    assert AlignmentService._detect_unnarrated_spans(anchors, text) == []


def test_budget_bars_exclusion_when_audio_covers_the_text():
    # A gap-looking pair (big char jump, tiny audio, normal per-word timing) that
    # passes every local check — but the book's audio (with a slow recovery stretch)
    # is long enough to have narrated all the text, so it is a lexical-timing artifact,
    # not a real gap. Density alone would exclude it; the audio-shortfall budget must not.
    text = "word " * 800  # 4000 chars
    anchors = [
        {"char": 0, "ts": 0.0, "t_idx": 0},
        {"char": 200, "ts": 5.0, "t_idx": 40},
        {"char": 400, "ts": 10.0, "t_idx": 80},
        {"char": 3400, "ts": 12.0, "t_idx": 92},    # 3000 chars over 2s, 12 words: looks like a gap
        {"char": 3600, "ts": 300.0, "t_idx": 132},  # slow recovery: audio actually covers the text
        {"char": 3800, "ts": 305.0, "t_idx": 172},
    ]
    assert AlignmentService._detect_unnarrated_spans(anchors, text) == []
    # Same map, but with the recovery stretch removed so the audio really is short:
    # now the shortfall budget admits the gap.
    short = anchors[:4] + [{"char": 3800, "ts": 17.0, "t_idx": 172}]
    assert AlignmentService._detect_unnarrated_spans(short, text) == [(459, 3400)]


@pytest.mark.parametrize("kind", ["poor_coverage", "synthetic_only", "backward", "nan", "empty"])
def test_unreliable_lexical_map_is_not_used(kind):
    text, anchors, _ = lexical_gap()
    if kind == "poor_coverage":
        anchors[-1]["ts"] = 1000
    elif kind == "synthetic_only":
        for point in anchors:
            point.pop("t_idx", None)
    elif kind == "backward":
        anchors[10]["ts"] = anchors[9]["ts"] - 1
    elif kind == "nan":
        anchors[10]["ts"] = float("nan")
    else:
        anchors = []
    assert AlignmentService._detect_unnarrated_spans(anchors, text) == []


def test_legacy_offsets():
    text, anchors, span = lexical_gap()
    for point in anchors:
        point["global_char"] = point.pop("char")
    assert AlignmentService._detect_unnarrated_spans(anchors, text) == [span]


@pytest.mark.parametrize("seconds,words", [(0, 12), (0.0464, 24), (0.13868, 13)])
def test_compressed_transcript_time_does_not_prove_unnarrated_text(seconds, words):
    # Live Dao Divinity: 24 transcript words in 0.0464s and 13 in 0.13868s.
    # Both flagged passages are audibly narrated; the old lexical timing is bad.
    text, anchors, _ = lexical_gap(seconds=seconds)
    for point in anchors[21:-1]:
        point['t_idx'] += words - 1
    assert AlignmentService._detect_unnarrated_spans(anchors, text) == []


def test_adjacent_flags_do_not_swallow_the_shared_matched_word():
    text, anchors, first = lexical_gap()
    for point in anchors[22:]:
        point["char"] += 2000
    text = text[:6000] + "word " * 400 + text[6000:]
    anchors[22]["ts"] = anchors[21]["ts"] + 2
    spans = AlignmentService._detect_unnarrated_spans(anchors, text)
    assert spans == [first, (6059, 8200)]


@pytest.mark.parametrize("spans,expected", [
    (None, 0.6),
    ([(200, 800)], 0.25),
    ([(500, 800), (200, 600), (300, 500)], 0.25),
    ([(-100, 100), (1000, 1200)], 600 / 900),
    ([(0, 1000)], 1.0),
])
def test_gap_fraction_subtracts_clipped_union_from_gap_and_extent(spans, expected):
    points = [{"char": c, "ts": c / 10} for c in [0, 100, 200, 800, 900, 1000]]
    assert AlignmentService._max_gap_fraction(points, spans) == pytest.approx(expected)


@pytest.fixture
def service(tmp_path):
    db = DatabaseService(str(tmp_path / "unnarrated.db"))
    try:
        yield AlignmentService(db, Polisher())
    finally:
        db.db_manager.close()


def test_service_detects_excludes_accepts_and_backs_up_large_intentional_gap(service, caplog):
    text, prior, span = lexical_gap(gap=6000)
    service._save_alignment("book", prior, "lexical", total_chars=len(text))
    # Realistic CTC output: the intentionally-unnarrated span costs no real audio
    # time, so ts stays a continuous function of the *collapsed* (post-exclusion)
    # char position rather than the raw one -- otherwise the map's own pacing
    # would look pathologically uneven (`map_quality`'s density-spread axis) purely
    # as an artifact of this synthetic construction, not a real alignment defect.
    gap_width = span[1] - span[0]
    new = [{"char": c, "ts": (c if c < span[0] else c - gap_width) / 40}
           for c in range(0, len(text) + 1, 5) if not span[0] <= c < span[1]]
    assert not service._ctc_map_accepted("book", new)
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "align", return_value=new) as align, \
         caplog.at_level("INFO"):
        assert service.align_forced_and_store("book", "/audio.m4b", text)
    assert align.call_args.kwargs["exclude_spans"] == [span]
    assert "CTC: excluding likely unnarrated interior text" in caplog.text
    assert service.database_service.get_alignment_method("book") == "ctc"
    assert service.restore_previous_alignment("book")
    assert service._get_alignment("book") == prior


def test_intentional_gap_does_not_hide_a_separate_failed_chunk(service):
    points = [{"char": c, "ts": c / 10} for c in [0, 10, 20, 60, 70, 100]]
    assert not service._ctc_map_accepted("book", points, [(20, 60)])


def test_exclusions_apply_to_incumbent_and_challenger_alike(service):
    # The regression comparison now lives in `_publish_map` (issue #426 phase 2).
    # Subtracting the exclusion only from the challenger would make its 20% narrated
    # gap look worse than the incumbent's unadjusted 10%, spuriously vetoing a map
    # that is really an equivalent, evenly-paced rebuild.
    prior = [{"char": c, "ts": c / 10} for c in range(0, 1001, 100)]
    new = [p for p in prior if not 200 < p["char"] < 700]
    service._save_alignment("book", prior, "lexical", total_chars=1000)
    assert service._publish_map("book", new, "ctc", total_chars=1000, exclude_spans=[(200, 700)])
    assert service.database_service.get_alignment_method("book") == "ctc"


def test_mismatched_ebook_length_prevents_detection(service):
    text, prior, _ = lexical_gap()
    service._save_alignment("book", prior, "lexical", total_chars=len(text) - 1)
    with patch.object(ForcedAligner, "is_available", return_value=True), \
         patch.object(ForcedAligner, "align", return_value=None) as align:
        assert not service.align_forced_and_store("book", "/audio.m4b", text)
    assert align.call_args.kwargs["exclude_spans"] == []


def test_real_lexical_ngram_preserves_all_twelve_narrated_words(service):
    words = [f"term{i:04d}" for i in range(1000)]
    left = " ".join(words[:495]) + " *** " + " ".join(words[495:500])
    skipped = "UNNARRATED " * 200
    text = left + " " + skipped + " ".join(words[500:])
    segments = [{"start": i * 0.2, "end": (i + 1) * 0.2, "text": word}
                for i, word in enumerate(words)]
    prior, method, _map_segments = service._generate_alignment_map_with_method(segments, text)
    assert method == "lexical"
    spans = service._detect_unnarrated_spans(prior, text)
    assert spans == [(len(left), len(left) + 1 + len(skipped))]
