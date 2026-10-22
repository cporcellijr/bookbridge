"""Regression tests for issue #540 — Whisper resume ignored unsplit parts.

Parts shorter than the split length stay ``part_NNN.wav``; only longer ones become
``part_NNN_split_XXX.wav``. The resume path globbed for split files alone, so the
reporter's 63-part book (4 parts split into 8 pieces) resumed with 8 of its 67
chunks, the coverage guard rejected 12989s of 85453s, and the cache wipe took
``_progress.json`` with it: 35 finished chunks lost.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.utils.polisher import Polisher
from src.utils.transcriber import AudioTranscriber

# The reporter's Book A: 63 parts, 85453s; four parts over 45 min were split in two.
BOOK_SECONDS = 85453.0
LONG_PARTS = {5: 46.5 * 60, 17: 46.0 * 60, 23: 68.2 * 60, 48: 55.8 * 60}
PART_COUNT = 63
SHORT_PART_SECONDS = (BOOK_SECONDS - sum(LONG_PARTS.values())) / (PART_COUNT - len(LONG_PARTS))


class WhisperResumeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.transcriber = AudioTranscriber(self.tmp, MagicMock(), Polisher())
        self._saved_env = os.environ.pop("TRANSCRIPT_MIN_COVERAGE", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["TRANSCRIPT_MIN_COVERAGE"] = self._saved_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache_dir(self, abs_id):
        cache_dir = self.tmp / "audio_cache" / abs_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _provider(self):
        """Each chunk transcribes to one segment spanning the chunk's measured length."""
        provider = MagicMock()
        provider.supports_raw_audio = False
        provider.get_name.return_value = "test"
        provider.last_vad_filter = False
        provider.transcribe.side_effect = lambda path: [{
            "start": 0.0, "end": self.transcriber.get_audio_duration(path), "text": Path(path).name,
        }]
        return provider


class TestResumeSeesUnsplitParts(WhisperResumeTestCase):
    def _write_reported_cache(self, abs_id):
        """63 parts as the transcriber leaves them: 59 whole, 4 split in two."""
        cache_dir = self._cache_dir(abs_id)
        durations = {}
        for idx in range(PART_COUNT):
            if idx in LONG_PARTS:
                for piece in (1, 2):
                    name = f"part_{idx:03d}_split_{piece:03d}.wav"
                    durations[name] = LONG_PARTS[idx] / 2
            else:
                durations[f"part_{idx:03d}.wav"] = SHORT_PART_SECONDS
        for name in durations:
            (cache_dir / name).write_bytes(b"RIFF")
        return cache_dir, durations

    def test_reported_resume_continues_from_chunk_36_of_67(self):
        abs_id = "book-a"
        cache_dir, durations = self._write_reported_cache(abs_id)
        chunk_order = sorted(durations)
        done_seconds = sum(durations[name] for name in chunk_order[:35])
        progress_file = cache_dir / "_progress.json"
        progress_file.write_text(json.dumps({
            "chunks_completed": 35,
            "cumulative_duration": done_seconds,
            "transcript": [{"start": 0.0, "end": done_seconds, "text": "first 35 chunks"}],
            "done": False,
        }))
        self.transcriber.get_audio_duration = MagicMock(side_effect=lambda p: durations[Path(p).name])
        audio_urls = [{"stream_url": f"http://abs/{idx}.mp3", "ext": "mp3"} for idx in range(PART_COUNT)]
        provider = self._provider()

        with patch("src.utils.transcriber.get_transcription_provider", return_value=provider), \
                patch("src.utils.transcriber.requests.get") as download, \
                self.assertLogs("src.utils.transcriber", level="INFO") as logs:
            transcript = self.transcriber.process_audio(
                abs_id, audio_urls, expected_duration=BOOK_SECONDS,
            )

        output = "\n".join(logs.output)
        self.assertIn("♻️ Resuming transcription: 35 chunks previously done", output)
        self.assertIn("✅ All parts cached. Starting transcription (67 chunks)...", output)
        self.assertNotIn("Starting transcription (8 chunks)", output)
        self.assertNotIn("Coverage too low", output)
        download.assert_not_called()
        transcribed = [Path(call.args[0]).name for call in provider.transcribe.call_args_list]
        self.assertEqual(transcribed, chunk_order[35:])
        self.assertEqual(transcript[0]["text"], "first 35 chunks")
        self.assertEqual(len(transcript), 1 + 32)
        self.assertEqual(json.loads(progress_file.read_text())["chunks_completed"], 67)

    def test_complete_cache_without_progress_is_reused_instead_of_redownloaded(self):
        abs_id = "complete-cache"
        cache_dir = self._cache_dir(abs_id)
        names = ["part_000.wav", "part_001_split_001.wav", "part_001_split_002.wav",
                 "part_002_normalized.wav"]
        for name in names:
            (cache_dir / name).write_bytes(b"RIFF")
        self.transcriber.get_audio_duration = MagicMock(return_value=25.0)
        audio_urls = [{"stream_url": f"http://abs/{idx}.mp3", "ext": "mp3"} for idx in range(3)]
        provider = self._provider()

        with patch("src.utils.transcriber.get_transcription_provider", return_value=provider), \
                patch("src.utils.transcriber.requests.get") as download, \
                self.assertLogs("src.utils.transcriber", level="INFO") as logs:
            self.transcriber.process_audio(abs_id, audio_urls, expected_duration=100.0)

        self.assertIn(
            "♻️ Found valid cache (4 files covering all 3 parts). Skipping download.",
            "\n".join(logs.output),
        )
        download.assert_not_called()
        self.assertEqual([Path(c.args[0]).name for c in provider.transcribe.call_args_list], names)


class TestProgressSurvivesAudioWipes(WhisperResumeTestCase):
    def _write_progress(self, cache_dir, chunks_completed, seconds):
        progress_file = cache_dir / "_progress.json"
        progress_file.write_text(json.dumps({
            "chunks_completed": chunks_completed,
            "cumulative_duration": seconds,
            "transcript": [{"start": 0.0, "end": seconds, "text": "done earlier"}],
            "done": False,
        }))
        return progress_file

    def test_coverage_rejection_clears_audio_but_keeps_the_transcript(self):
        abs_id = "short-on-resume"
        cache_dir = self._cache_dir(abs_id)
        for name in ("part_000.wav", "part_001.wav"):
            (cache_dir / name).write_bytes(b"RIFF")
        progress_file = self._write_progress(cache_dir, 1, 10.0)
        self.transcriber.get_audio_duration = MagicMock(return_value=10.0)
        audio_urls = [{"stream_url": f"http://abs/{idx}.mp3", "ext": "mp3"} for idx in range(2)]
        provider = self._provider()

        with patch("src.utils.transcriber.get_transcription_provider", return_value=provider),                 patch("src.utils.transcriber.requests.get") as download:
            with self.assertRaises(ValueError) as ctx:
                self.transcriber.process_audio(abs_id, audio_urls, expected_duration=100.0)

        self.assertIn("Coverage too low", str(ctx.exception))
        download.assert_not_called()
        provider.transcribe.assert_not_called()
        self.assertEqual(sorted(p.name for p in cache_dir.iterdir()), ["_progress.json"])
        self.assertEqual(json.loads(progress_file.read_text())["chunks_completed"], 1)

    @patch("src.utils.transcriber.requests.get")
    def test_wiping_a_partial_cache_keeps_the_transcript_through_the_redownload(self, download):
        abs_id = "partial-cache"
        cache_dir = self._cache_dir(abs_id)
        (cache_dir / "part_000_split_001.wav").write_bytes(b"RIFF")
        progress_file = self._write_progress(cache_dir, 1, 50.0)
        progress_during_download = []

        def respond(*_args, **_kwargs):
            progress_during_download.append(progress_file.exists())
            response = MagicMock()
            response.iter_content.return_value = [b"audio data"]
            response.headers = {"Content-Length": str(len(b"audio data"))}
            context = MagicMock()
            context.__enter__.return_value = response
            return context

        download.side_effect = respond

        def normalize(path):
            out = path.with_suffix(".wav")
            out.write_bytes(b"RIFF")
            path.unlink()
            return out

        self.transcriber.normalize_audio_to_wav = MagicMock(side_effect=normalize)
        self.transcriber.get_audio_duration = MagicMock(return_value=50.0)
        audio_urls = [{"stream_url": f"http://abs/{idx}.mp3", "ext": "mp3"} for idx in range(2)]
        provider = self._provider()

        with patch("src.utils.transcriber.get_transcription_provider", return_value=provider):
            transcript = self.transcriber.process_audio(abs_id, audio_urls, expected_duration=100.0)

        self.assertEqual(progress_during_download, [True, True])
        self.assertEqual([Path(c.args[0]).name for c in provider.transcribe.call_args_list], ["part_001.wav"])
        self.assertEqual(transcript[0]["text"], "done earlier")


class TestCachedChunks(WhisperResumeTestCase):
    def _chunks(self, names, part_count):
        cache_dir = self._cache_dir("chunks")
        for name in names:
            (cache_dir / name).write_bytes(b"x")
        result = AudioTranscriber._cached_chunks(cache_dir, part_count)
        return None if result is None else [p.name for p in result]

    def test_orders_whole_and_split_parts_like_a_fresh_run(self):
        names = ["part_018.wav", "part_017_split_002.wav", "part_016.wav", "part_017_split_001.wav"]
        expected = ["part_016.wav", "part_017_split_001.wav", "part_017_split_002.wav", "part_018.wav"]
        cache_dir = self._cache_dir("chunks")
        for idx in range(16):
            (cache_dir / f"part_{idx:03d}.wav").write_bytes(b"x")
        self.assertEqual(self._chunks(names, 19)[16:], expected)

    def test_orders_by_part_number_past_999(self):
        names = [f"part_{idx:03d}.wav" for idx in range(1001)]
        self.assertEqual(self._chunks(names, 1001)[-2:], ["part_999.wav", "part_1000.wav"])

    def test_ignores_progress_and_hidden_staging_files(self):
        names = ["part_000.wav", "_progress.json", ".part_001.mp3.abc.partial", "part_001_normalized.wav"]
        self.assertEqual(self._chunks(names, 2), ["part_000.wav", "part_001_normalized.wav"])

    def test_interrupted_or_incomplete_caches_are_rejected(self):
        cases = {
            "normalization interrupted (source left behind)": ["part_000.wav", "part_001.mp3", "part_001.wav"],
            "wav normalization interrupted": ["part_000.wav", "part_001.wav", "part_001_normalized.wav"],
            "split interrupted": ["part_000.wav", "part_001.wav", "part_001_split_001.wav"],
            "a part missing": ["part_000.wav"],
            "an extra part": ["part_000.wav", "part_001.wav", "part_002.wav"],
        }
        for label, names in cases.items():
            with self.subTest(label):
                shutil.rmtree(self.tmp / "audio_cache" / "chunks", ignore_errors=True)
                self.assertIsNone(self._chunks(names, 2))


if __name__ == "__main__":
    unittest.main()
