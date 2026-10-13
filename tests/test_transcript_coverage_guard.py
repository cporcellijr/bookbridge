"""Regression tests for issue #362 — long audiobooks silently truncated.

The reporter's 28h audiobook (101,665.44s) was transcribed as if it were 21.3h
(76,726.9s). Nothing noticed: the transcript aligned cleanly against the ebook, so
every position derived from that map was wrong by the coverage shortfall while the
job reported success.

Three guards are covered here:
  * a streamed download that stops short of its Content-Length is rejected,
  * audio shorter than the runtime the library reports is rejected BEFORE Whisper
    runs (a bad download must not cost hours),
  * a completed `_progress.json` holding a short transcript is discarded rather
    than replayed forever — `_prune_audio_cache` keeps that file deliberately, so
    without this an already-affected book never heals.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.transcriber import AudioTranscriber

# The reporter's actual numbers (issue #362).
REPORTED_AUDIO_DURATION = 101665.44
REPORTED_TRANSCRIPT_EXTENT = 76726.928


def _segments_to(end_ts):
    return [{"start": 0.0, "end": end_ts, "text": "hello there"}]


class TranscriptCoverageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.transcriber = AudioTranscriber(self.tmp, MagicMock(), MagicMock())
        self._saved_env = os.environ.get("TRANSCRIPT_MIN_COVERAGE")
        os.environ.pop("TRANSCRIPT_MIN_COVERAGE", None)

    def tearDown(self):
        if self._saved_env is None:
            os.environ.pop("TRANSCRIPT_MIN_COVERAGE", None)
        else:
            os.environ["TRANSCRIPT_MIN_COVERAGE"] = self._saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCoverageThresholdSetting(TranscriptCoverageTestCase):
    def test_defaults_to_smil_threshold(self):
        self.assertEqual(self.transcriber.min_coverage, 0.85)

    def test_reads_setting_per_access(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "0.5"
        self.assertEqual(self.transcriber.min_coverage, 0.5)
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "0.99"
        self.assertEqual(self.transcriber.min_coverage, 0.99)

    def test_invalid_value_falls_back_to_default(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "not-a-number"
        self.assertEqual(self.transcriber.min_coverage, 0.85)

    def test_clamped_to_unit_interval(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "5"
        self.assertEqual(self.transcriber.min_coverage, 1.0)
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "-1"
        self.assertEqual(self.transcriber.min_coverage, 0.0)


class TestCoverageCheck(TranscriptCoverageTestCase):
    def test_rejects_the_reported_shortfall(self):
        with self.assertRaises(ValueError) as ctx:
            self.transcriber._check_audio_coverage(
                REPORTED_TRANSCRIPT_EXTENT, REPORTED_AUDIO_DURATION
            )
        message = str(ctx.exception)
        self.assertIn("Coverage too low", message)
        self.assertIn("75.5%", message)
        self.assertIn("101665", message)
        self.assertIn("76727", message)

    def test_accepts_full_coverage(self):
        self.transcriber._check_audio_coverage(
            REPORTED_AUDIO_DURATION, REPORTED_AUDIO_DURATION
        )

    def test_accepts_small_shortfall_within_threshold(self):
        self.transcriber._check_audio_coverage(
            REPORTED_AUDIO_DURATION * 0.9, REPORTED_AUDIO_DURATION
        )

    def test_zero_threshold_disables_the_guard(self):
        os.environ["TRANSCRIPT_MIN_COVERAGE"] = "0"
        self.transcriber._check_audio_coverage(
            REPORTED_TRANSCRIPT_EXTENT, REPORTED_AUDIO_DURATION
        )

    def test_unknown_expected_duration_is_not_an_error(self):
        """Audio-only or legacy mappings may have no recorded runtime."""
        self.transcriber._check_audio_coverage(REPORTED_TRANSCRIPT_EXTENT, None)
        self.transcriber._check_audio_coverage(REPORTED_TRANSCRIPT_EXTENT, 0)

    def test_missing_actual_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            self.transcriber._check_audio_coverage(0.0, REPORTED_AUDIO_DURATION)

    def test_transcript_extent_reads_last_segment(self):
        self.assertEqual(
            self.transcriber._transcript_extent(_segments_to(REPORTED_TRANSCRIPT_EXTENT)),
            REPORTED_TRANSCRIPT_EXTENT,
        )
        self.assertEqual(self.transcriber._transcript_extent([]), 0.0)
        self.assertEqual(self.transcriber._transcript_extent(None), 0.0)

    def test_cached_word_transcript_uses_configured_coverage_and_validates_snapshot(self):
        cache_dir = self.transcriber.cache_root / "cached-book"
        cache_dir.mkdir(parents=True)
        segments = [{
            "start": 0.0,
            "end": 90.0,
            "text": "ready",
            "words": [{"word": "ready", "start": 89.0, "end": 90.0}],
        }]
        progress = {"done": True, "chunks_completed": 1, "transcript": segments}
        (cache_dir / "_progress.json").write_text(json.dumps(progress), encoding="utf-8")

        assert self.transcriber.get_cached_word_timed_transcript("cached-book", 100) == segments
        self.assertIsNone(
            self.transcriber.get_cached_word_timed_transcript("cached-book", 110)
        )

        progress.update(vad_filter=True, cumulative_duration=95.0)
        progress["transcript"][0]["words"][0].update(start=69.0, end=70.0)
        (cache_dir / "_progress.json").write_text(json.dumps(progress), encoding="utf-8")
        assert self.transcriber.get_cached_word_timed_transcript("cached-book", 100) == segments

        progress["transcript"][0]["words"][0]["end"] = float("nan")
        (cache_dir / "_progress.json").write_text(json.dumps(progress), encoding="utf-8")
        self.assertIsNone(
            self.transcriber.get_cached_word_timed_transcript("cached-book", 100)
        )


class TestDownloadIntegrity(TranscriptCoverageTestCase):
    def _response(self, content_length):
        response = MagicMock()
        response.headers = {"Content-Length": str(content_length)} if content_length else {}
        return response

    def test_truncated_download_raises(self):
        target = self.tmp / "part_000.m4b"
        target.write_bytes(b"x" * 500)
        with self.assertRaises(ValueError) as ctx:
            self.transcriber._verify_download_size(self._response(1000), target)
        self.assertIn("Truncated download", str(ctx.exception))

    def test_complete_download_passes(self):
        target = self.tmp / "part_000.m4b"
        target.write_bytes(b"x" * 1000)
        self.transcriber._verify_download_size(self._response(1000), target)

    def test_missing_content_length_is_not_an_error(self):
        target = self.tmp / "part_000.m4b"
        target.write_bytes(b"x" * 10)
        self.transcriber._verify_download_size(self._response(None), target)


class TestProcessAudioGuards(TranscriptCoverageTestCase):
    """Drives the real process_audio() entry point, not just the predicate."""

    def _write_completed_cache(self, abs_id, transcript_end):
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        progress_file = cache_dir / "_progress.json"
        progress_file.write_text(json.dumps({
            "chunks_completed": 29,
            "cumulative_duration": transcript_end,
            "transcript": _segments_to(transcript_end),
            "done": True,
        }))
        return progress_file

    def test_short_cached_transcript_is_discarded_and_not_returned(self):
        """The affected install must heal instead of replaying the bad cache."""
        abs_id = "dungeon-crawler-carl"
        progress_file = self._write_completed_cache(abs_id, REPORTED_TRANSCRIPT_EXTENT)

        with patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider_factory.return_value = provider
            with self.assertRaises(Exception):
                self.transcriber.process_audio(
                    abs_id, [], expected_duration=REPORTED_AUDIO_DURATION
                )

        self.assertFalse(
            progress_file.exists(),
            "a below-coverage cached transcript must be unlinked so the retry re-downloads",
        )

    def test_good_cached_transcript_is_still_reused(self):
        abs_id = "healthy-book"
        self._write_completed_cache(abs_id, REPORTED_AUDIO_DURATION)

        result = self.transcriber.process_audio(
            abs_id, [], expected_duration=REPORTED_AUDIO_DURATION
        )
        self.assertEqual(len(result), 1)

    def test_vad_cache_uses_processed_audio_duration_when_music_follows_speech(self):
        abs_id = 'vad-trailing-music'
        progress_file = self._write_completed_cache(abs_id, 60)
        progress = json.loads(progress_file.read_text())
        progress.update(vad_filter=True, cumulative_duration=100)
        progress_file.write_text(json.dumps(progress))
        result = self.transcriber.process_audio(abs_id, [], expected_duration=100)
        self.assertEqual(result[0]['end'], 60)
        self.assertTrue(progress_file.exists())

    def test_vad_does_not_accept_audio_shorter_than_expected_runtime(self):
        abs_id = 'vad-short-audio'
        progress_file = self._write_completed_cache(abs_id, 60)
        progress = json.loads(progress_file.read_text())
        progress.update(vad_filter=True, cumulative_duration=60)
        progress_file.write_text(json.dumps(progress))
        with self.assertRaises(Exception):
            self.transcriber.process_audio(abs_id, [], expected_duration=100)
        self.assertFalse(progress_file.exists())

    def test_cached_transcript_reused_when_no_expected_duration(self):
        """Unchanged behaviour for callers that cannot supply a runtime."""
        abs_id = "legacy-book"
        self._write_completed_cache(abs_id, REPORTED_TRANSCRIPT_EXTENT)

        result = self.transcriber.process_audio(abs_id, [])
        self.assertEqual(len(result), 1)

    def test_short_audio_rejected_before_any_transcription(self):
        """Fail fast: a truncated download must not cost hours of Whisper."""
        abs_id = "short-audio-book"
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        chunk = cache_dir / "part_000_split_001.wav"
        chunk.write_bytes(b"RIFF")

        self.transcriber.get_audio_duration = MagicMock(return_value=REPORTED_TRANSCRIPT_EXTENT)

        with patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider.get_name.return_value = "test"
            provider_factory.return_value = provider

            with self.assertRaises(ValueError) as ctx:
                self.transcriber.process_audio(
                    abs_id,
                    [{"stream_url": "http://example.com/1.m4b", "ext": "m4b"}],
                    expected_duration=REPORTED_AUDIO_DURATION,
                )

            self.assertIn("Coverage too low", str(ctx.exception))
            provider.transcribe.assert_not_called()

        self.assertFalse(
            cache_dir.exists(),
            "the short audio cache must be cleared so the retry re-downloads",
        )


if __name__ == "__main__":
    unittest.main()


class TestStageAttribution(TranscriptCoverageTestCase):
    """#362 follow-up: say WHICH stage lost the audio, not just that it's short.

    The reporter's second run proved the guard works but left the cause open —
    the log showed a short WAV without saying whether the download or the
    normalization produced it.
    """

    def test_message_names_the_stage(self):
        with self.assertRaises(ValueError) as ctx:
            self.transcriber._check_audio_coverage(
                REPORTED_TRANSCRIPT_EXTENT, REPORTED_AUDIO_DURATION,
                stage="source audio as downloaded",
            )
        message = str(ctx.exception)
        # The greppable prefix is a contract; the stage is a suffix on it.
        self.assertTrue(message.startswith("TRANSCRIPT REJECTED: Coverage too low"))
        self.assertIn("[source audio as downloaded]", message)

    def test_stage_is_optional(self):
        with self.assertRaises(ValueError) as ctx:
            self.transcriber._check_audio_coverage(
                REPORTED_TRANSCRIPT_EXTENT, REPORTED_AUDIO_DURATION)
        self.assertNotIn("[", str(ctx.exception))

    def test_short_source_is_blamed_on_the_download_not_normalization(self):
        """A source already short must fail before normalization is implicated."""
        abs_id = "short-source"
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)

        self.transcriber.get_audio_duration = MagicMock(return_value=REPORTED_TRANSCRIPT_EXTENT)
        self.transcriber.normalize_audio_to_wav = MagicMock(
            side_effect=AssertionError("normalization must not run on a short source"))

        def fake_get(url, **kwargs):
            response = MagicMock()
            response.headers = {}
            response.iter_content.return_value = [b"audio"]
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *a: False
            return response

        with patch("src.utils.transcriber.requests.get", side_effect=fake_get), \
             patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider_factory.return_value = provider

            with self.assertRaises(ValueError) as ctx:
                self.transcriber.process_audio(
                    abs_id,
                    [{"stream_url": "http://example.com/1.m4b", "ext": "m4b"}],
                    expected_duration=REPORTED_AUDIO_DURATION,
                )

        self.assertIn("source audio as downloaded", str(ctx.exception))

    def test_normalization_shrinkage_is_reported(self):
        """FFmpeg can stop early on a damaged stream and still exit 0."""
        abs_id = "lossy-normalize"
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        wav = cache_dir / "part_000.wav"
        wav.write_bytes(b"RIFF")

        durations = {"source": REPORTED_AUDIO_DURATION, "wav": REPORTED_TRANSCRIPT_EXTENT}
        self.transcriber.get_audio_duration = MagicMock(
            side_effect=lambda p: durations["wav"] if str(p).endswith(".wav") else durations["source"])
        self.transcriber.normalize_audio_to_wav = MagicMock(return_value=wav)

        def fake_get(url, **kwargs):
            response = MagicMock()
            response.headers = {}
            response.iter_content.return_value = [b"audio"]
            response.__enter__ = lambda s: s
            response.__exit__ = lambda *a: False
            return response

        with patch("src.utils.transcriber.requests.get", side_effect=fake_get), \
             patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider_factory.return_value = provider

            with self.assertRaises(ValueError) as ctx:
                self.transcriber.process_audio(
                    abs_id,
                    [{"stream_url": "http://example.com/1.m4b", "ext": "m4b"}],
                    expected_duration=REPORTED_AUDIO_DURATION,
                )

        self.assertIn("Normalization lost audio", str(ctx.exception))


class TestLargeWavHeaders(TranscriptCoverageTestCase):
    """A 16kHz mono WAV passes RIFF's 32-bit size fields at ~37.3h of audio.

    ffmpeg then warns "output file will be broken". Current builds still read
    such a file, but the standards-correct answer is RF64, and it costs nothing
    below the limit.
    """

    def _ffmpeg_cmd_for(self, method, *args):
        with patch("src.utils.transcriber.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="1.0", stderr="")
            try:
                method(*args)
            except Exception:
                pass
            return [call.args[0] for call in run.call_args_list if call.args] or []

    def test_normalize_requests_rf64_auto(self):
        source = self.tmp / "part_000.m4b"
        source.write_bytes(b"x")
        cmds = self._ffmpeg_cmd_for(self.transcriber.normalize_audio_to_wav, source)
        self.assertTrue(cmds, "expected an ffmpeg invocation")
        cmd = cmds[0]
        self.assertIn("-rf64", cmd)
        self.assertEqual(cmd[cmd.index("-rf64") + 1], "auto")

    def test_split_requests_rf64_auto(self):
        source = self.tmp / "part_000.wav"
        source.write_bytes(b"x")
        self.transcriber.get_audio_duration = MagicMock(return_value=10_000.0)
        cmds = self._ffmpeg_cmd_for(self.transcriber.split_audio_file, source, 2700)
        ffmpeg_cmds = [c for c in cmds if c and c[0] == "ffmpeg"]
        self.assertTrue(ffmpeg_cmds, "expected ffmpeg split invocations")
        for cmd in ffmpeg_cmds:
            self.assertIn("-rf64", cmd)
            self.assertEqual(cmd[cmd.index("-rf64") + 1], "auto")

    def test_riff_limit_is_where_we_think_it_is(self):
        """Documents the threshold the flag exists for: 16kHz mono s16le."""
        bytes_per_second = 16000 * 1 * 2
        limit_hours = (2 ** 32) / bytes_per_second / 3600
        self.assertAlmostEqual(limit_hours, 37.28, places=1)


class TestContainerMetadataIsNotAMeasurement(TranscriptCoverageTestCase):
    """Diagnostics finding 4441 — "Howl's Moving Castle", a 36-part audiobook.

    The reported failure, verbatim:

        Normalization lost audio for part 1: source 30913s -> WAV 669s

    Part 1 of 36 was 5,353,264 bytes. At the container's claimed 30,913s that is
    1.4 kbps, which no MP3 encoder produces; at the decoded 669s it is exactly
    64.0 kbps. An ffprobe container duration is a declaration (Xing/ID3 header),
    not a measurement — here it lied, the decode was right, and the job died on
    part 1, so parts 2-36 were never downloaded and the book could never
    transcribe. Only the decoded audio, judged against the runtime the library
    reports, may fail a job.
    """

    CONTAINER_SECONDS = 30913.0
    DECODED_SECONDS = 669.0
    PART_COUNT = 36
    TRUE_RUNTIME = DECODED_SECONDS * PART_COUNT  # 24,084s — what actually decodes

    def _arrange(self, part_count):
        """Container metadata claims 8.6h per part; each part decodes to 11 min."""
        self.transcriber.get_audio_duration = MagicMock(
            side_effect=lambda p: (
                self.DECODED_SECONDS if str(p).endswith(".wav") else self.CONTAINER_SECONDS
            )
        )
        self.transcriber.normalize_audio_to_wav = MagicMock(
            side_effect=lambda p: p.with_suffix(".wav"))
        self.transcriber.split_audio_file = MagicMock(side_effect=lambda p, _max: [p])
        return [
            {"stream_url": f"http://example.com/{i}.mp3", "ext": "mp3"}
            for i in range(part_count)
        ]

    @staticmethod
    def _fake_get(url, **kwargs):
        response = MagicMock()
        response.headers = {}
        response.iter_content.return_value = [b"audio"]
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        return response

    def _run(self, audio_urls, expected_duration):
        with patch("src.utils.transcriber.requests.get", side_effect=self._fake_get),              patch("src.utils.transcriber.get_transcription_provider") as provider_factory:
            provider = MagicMock()
            provider.supports_raw_audio = False
            provider.get_name.return_value = "test"
            provider.transcribe.return_value = [
                {"start": 0.0, "end": self.DECODED_SECONDS, "text": "hello there"}
            ]
            provider_factory.return_value = provider
            return self.transcriber.process_audio(
                "howls-moving-castle", audio_urls, expected_duration=expected_duration,
            )

    def test_lying_container_metadata_does_not_fail_the_book(self):
        """The reported case: the decoded audio matches the runtime, so it must run."""
        audio_urls = self._arrange(self.PART_COUNT)

        transcript = self._run(audio_urls, self.TRUE_RUNTIME)

        self.assertTrue(transcript, "the book must transcribe")
        self.assertEqual(
            self.transcriber.normalize_audio_to_wav.call_count, self.PART_COUNT,
            "every part must be downloaded and normalized, not just part 1",
        )

    def test_genuinely_short_audio_is_still_rejected(self):
        """The guard survives: decoded audio short of the runtime still fails."""
        audio_urls = self._arrange(self.PART_COUNT)

        with self.assertRaises(ValueError) as ctx:
            self._run(audio_urls, REPORTED_AUDIO_DURATION)

        message = str(ctx.exception)
        self.assertIn("Coverage too low", message)
        self.assertIn("[source audio as downloaded]", message)
        self.assertNotIn("Normalization lost audio", message)

    def test_shortfall_stays_fatal_without_a_library_runtime(self):
        """forge_service passes no expected_duration: the container is all we have."""
        audio_urls = self._arrange(1)

        with self.assertRaises(ValueError) as ctx:
            self._run(audio_urls, None)

        self.assertIn("Normalization lost audio for part 1", str(ctx.exception))
        self.assertIn("source 30913s -> WAV 669s", str(ctx.exception))
