"""Alignment and read-along audio follow the container's timeline.

What Lies in the Woods' m4b declares longer durations on 41 packets (its chapter
joins) than they decode to -- 1.57 s over the book. Players, ffmpeg seeks and
stream-copied read-along audio honour those durations; a plain decode drops them,
so the CTC map ran early after every join (1.3 s by the end against Whisper word
times). These tests build a small m4a with a 0.5 s timestamp gap and check every
decode site keeps it.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services.readalong_builder import _transcode_audio_for_embed
from src.utils.forced_aligner import (
    CONTAINER_TIMELINE_FILTER,
    ForcedAligner,
    container_timeline_filter,
)
from src.utils.quartznet_aligner import QuartzNetAligner


def _probe_result(**fields: str) -> subprocess.CompletedProcess:
    stdout = "".join(f"{k}={v}\n" for k, v in fields.items())
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class TestContainerTimelineDecision(unittest.TestCase):
    """Header-only decision; no ffmpeg needed."""

    def _decide(self, **fields: str):
        with patch("subprocess.run", return_value=_probe_result(**fields)):
            return container_timeline_filter("book.m4b")

    def test_chapter_join_gaps_get_the_filter(self):
        # What Lies in the Woods' real header: 1.57 s declared beyond its frames.
        self.assertEqual(self._decide(
            codec_name="aac", profile="LC", sample_rate="44100", time_base="1/44100",
            duration_ts="1790533199", nb_frames="1748500",
        ), CONTAINER_TIMELINE_FILTER)

    def test_packets_stamped_short_never_get_the_filter(self):
        # Dark Resurrection's shape: the container claims 121 s LESS than the
        # audio holds; the filter would drop that audio.
        self.assertIsNone(self._decide(
            codec_name="aac", profile="LC", sample_rate="44100", time_base="1/44100",
            duration_ts=str(int(26805.858 * 44100)), nb_frames="1159671",
        ))

    def test_ordinary_priming_trim_needs_no_filter(self):
        self.assertIsNone(self._decide(
            codec_name="aac", profile="LC", sample_rate="44100", time_base="1/44100",
            duration_ts=str(1000 * 1024 - 2112), nb_frames="1000",
        ))

    def test_he_aac_frames_count_2048_samples(self):
        self.assertIsNone(self._decide(
            codec_name="aac", profile="HE-AAC", sample_rate="44100", time_base="1/44100",
            duration_ts=str(1000 * 2048), nb_frames="1000",
        ))

    def test_non_aac_and_unprobeable_files_decode_as_before(self):
        self.assertIsNone(self._decide(codec_name="mp3", sample_rate="44100"))
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
            self.assertIsNone(container_timeline_filter("missing.m4b"))

_GAP_SECONDS = 0.5
_TONE_SECONDS = 2.0


def _make_gap_m4a(path: Path) -> None:
    """A 2 s tone whose second half is stamped 0.5 s later: a 2.5 s container
    timeline holding 2 s of decodable audio."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={_TONE_SECONDS}:sample_rate=44100",
         "-af", f"asetpts='PTS+if(gte(T,1),{_GAP_SECONDS}/TB,0)'",
         "-c:a", "aac", "-b:a", "64k", str(path)],
        check=True,
    )


def _plain_decoded_seconds(path: Path) -> float:
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    return len(raw) / 4 / 16000


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH -- decodes real audio")
class TestDecodeFollowsContainerTimeline(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.gap = self.dir / "gap.m4a"
        _make_gap_m4a(self.gap)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fixture_has_a_gap_a_plain_decode_drops(self):
        self.assertLess(_plain_decoded_seconds(self.gap), _TONE_SECONDS + 0.1)
        self.assertEqual(container_timeline_filter(self.gap), CONTAINER_TIMELINE_FILTER)

    def test_quartznet_windows_keep_the_container_gap(self):
        samples = sum(w.shape[0] for w in QuartzNetAligner._pcm_windows([self.gap]))
        self.assertAlmostEqual(samples / 16000, _TONE_SECONDS + _GAP_SECONDS, delta=0.06)

    def test_forced_aligner_decode_keeps_the_container_gap(self):
        aligner = ForcedAligner()
        try:
            num_samples = aligner._decode_to_memmap([self.gap]).shape[0]
        finally:
            aligner._cleanup_tmp_audio()
        self.assertAlmostEqual(num_samples / 16000, _TONE_SECONDS + _GAP_SECONDS, delta=0.06)

    def test_readalong_transcode_bakes_the_gap_into_the_audio(self):
        out = self.dir / "embed.m4a"
        self.assertTrue(_transcode_audio_for_embed([self.gap], "32k", out))
        self.assertAlmostEqual(_plain_decoded_seconds(out), _TONE_SECONDS + _GAP_SECONDS, delta=0.06)

    def test_multi_part_transcode_keeps_each_parts_gap(self):
        second = self.dir / "gap2.m4a"
        _make_gap_m4a(second)
        out = self.dir / "embed.m4a"
        self.assertTrue(_transcode_audio_for_embed([self.gap, second], "32k", out))
        self.assertAlmostEqual(_plain_decoded_seconds(out), 2 * (_TONE_SECONDS + _GAP_SECONDS), delta=0.12)


if __name__ == "__main__":
    unittest.main()
