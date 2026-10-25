"""find_slips flags stretches where text is squeezed into a moment and the audio after it has no text."""

from src.services.map_quality import find_slips

RATE = 14.0  # chars per second, a typical narration pace
HOURS = 3


def _map(points):
    return [{"char": int(c), "ts": float(t), "t_idx": i} for i, (c, t) in enumerate(points)]


def _steady():
    return [(c, c / RATE) for c in range(0, int(HOURS * 3600 * RATE), 50)]


def _slipped(at_seconds, minutes):
    """Pin the text narrated in `minutes` of audio to one moment, leaving that audio without text."""
    end = at_seconds + minutes * 60
    return [(c, at_seconds if at_seconds <= t < end else t) for c, t in _steady()]


def test_steady_narration_has_no_slips():
    assert find_slips(_map(_steady())) == []


def test_a_slip_is_found_where_the_audio_lost_its_text():
    for minutes in (5, 15):
        slips = find_slips(_map(_slipped(3600, minutes)))

        assert len(slips) == 1
        start, end = slips[0]
        assert abs(start - 3600) <= 180
        assert abs(end - (3600 + minutes * 60)) <= 180


def test_audio_without_text_alone_is_not_a_slip():
    # Music or an introduction the ebook doesn't have: ten minutes of audio, no text squeezed anywhere.
    points = [(c, t if t < 3600 else t + 600) for c, t in _steady()]

    assert find_slips(_map(points)) == []


def test_text_without_audio_alone_is_not_a_slip():
    # Notes or a code listing the narrator skips: 20,000 chars at one moment, no audio left without text.
    points = [(c if t < 3600 else c + 20000, t) for c, t in _steady()]

    assert find_slips(_map(points)) == []


def test_only_matched_anchors_count():
    # Interpolated points carry no transcript index and say nothing about the read-along's own timing.
    points = _map(_steady())
    points.append({"char": 70000, "ts": 10.0})

    assert find_slips(points) == []


def test_a_map_too_small_to_judge_has_no_slips():
    assert find_slips([]) == []
    assert find_slips(_map([(0, 0.0)])) == []
