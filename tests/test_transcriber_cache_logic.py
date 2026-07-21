import json
import unittest
from unittest.mock import MagicMock, patch, mock_open
import shutil
import os
import tempfile
from pathlib import Path
import sys

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from utils.transcriber import AudioTranscriber

class TestTranscriberCacheLogic(unittest.TestCase):
    def setUp(self):
        self.mock_data_dir = Path("/tmp/mock_data")
        self.mock_smil_extractor = MagicMock()
        self.mock_polisher = MagicMock()
        self.transcriber = AudioTranscriber(self.mock_data_dir, self.mock_smil_extractor, self.mock_polisher)
        
        # Mock dependencies that hit the network or filesystem
        self.transcriber.normalize_audio_to_wav = MagicMock()
        self.transcriber.split_audio_file = MagicMock()
        self.transcriber.get_audio_duration = MagicMock(return_value=100.0)
        
    @patch("src.utils.transcriber.requests.get")
    def test_partial_cache_triggers_redownload(self, mock_requests_get):
        """
        Bug Reproduction: 
        If we have 3 audio parts to download, but the cache only has part 0,
        the current logic sees 'some' files and skips the download.
        
        Expected Behavior:
        It should detect that parts 1 and 2 are missing, wipe the cache, and re-download everything.
        """
        # Setup
        abs_id = "test-book-id"
        audio_urls = [
            {'stream_url': 'http://example.com/1.mp3', 'ext': 'mp3'},
            {'stream_url': 'http://example.com/2.mp3', 'ext': 'mp3'},
            {'stream_url': 'http://example.com/3.mp3', 'ext': 'mp3'}
        ]
        
        # Setup real filesystem in mock_data_dir (which was set up in setUp)
        book_cache_dir = self.mock_data_dir / "audio_cache" / abs_id
        book_cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Create ONLY the first part to simulate incomplete cache
        # The transcriber looks for part_000_split_*.wav
        (book_cache_dir / "part_000_split_000.wav").touch()
        # Part 1 and 2 are missing
        
        # Setup dependencies
        with patch("src.utils.transcriber.get_transcription_provider") as mock_provider_getter:
            # Setup mock provider
            mock_provider = MagicMock()
            mock_provider.transcribe.return_value = []
            mock_provider_getter.return_value = mock_provider
            
            # Setup split implementation to create dummy files so the process continues
            # We need simple implementations that return valid Paths for the next steps
            
            # Mock requests response
            mock_response = MagicMock()
            mock_response.iter_content.return_value = [b"audio data"]
            mock_requests_get.return_value.__enter__.return_value = mock_response
            
            # Mock normalize: return a path that exists (we can just return the input path if we say it's wav)
            def mock_normalize(p):
                 # Return a path that "exists"
                 out = p.with_suffix('.wav')
                 out.touch()
                 return out
            self.transcriber.normalize_audio_to_wav.side_effect = mock_normalize
            
            def mock_split(path, duration):
                # Return the file itself as if it didn't need splitting
                return [path]
            self.transcriber.split_audio_file.side_effect = mock_split
            
            # Execute
            try:
                self.transcriber.process_audio(abs_id, audio_urls, progress_callback=MagicMock())
            except Exception as e:
                # print(f"Caught exception: {e}")
                pass

            # ASSERTION
            # In fixed version, it should detect missing parts, wipe cache (rmtree), and download 3 times.
            
            if mock_requests_get.call_count == 0:
                self.fail("Bug Reproduced: Download was skipped despite missing audio parts in cache.")
            
            self.assertEqual(mock_requests_get.call_count, 3, "Should download all 3 parts")

    def test_expected_long_audio_split_is_informational(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "long.mp3"
            audio_path.write_bytes(b"audio")
            self.transcriber.get_audio_duration.return_value = 2820.0

            with patch("utils.transcriber.logger") as mock_logger, \
                    patch("utils.transcriber.subprocess.run"):
                AudioTranscriber.split_audio_file(self.transcriber, audio_path)

        mock_logger.info.assert_any_call("⚠️ File 'long.mp3' is 47.0m — Splitting")
        self.assertFalse(any(
            call.args and "File 'long.mp3' is 47.0m" in str(call.args[0])
            for call in mock_logger.warning.call_args_list
        ))

    def test_completed_empty_cache_is_invalidated_and_retranscribed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            transcriber = AudioTranscriber(
                data_dir,
                self.mock_smil_extractor,
                self.mock_polisher,
            )
            source = data_dir / "source.mp3"
            source.write_bytes(b"audio")
            cache_dir = data_dir / "audio_cache" / "empty-cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "_progress.json").write_text(json.dumps({
                "chunks_completed": 1,
                "cumulative_duration": 10.0,
                "transcript": [],
                "done": True,
            }), encoding="utf-8")

            normalized = cache_dir / "part_000.wav"
            provider = MagicMock()
            provider.transcribe.return_value = [
                {"start": 0.0, "end": 1.0, "text": "hello"},
            ]
            transcriber.normalize_audio_to_wav = MagicMock(
                side_effect=lambda _path: normalized
            )
            transcriber.split_audio_file = MagicMock(return_value=[normalized])
            transcriber.get_audio_duration = MagicMock(return_value=10.0)

            with patch("utils.transcriber.get_transcription_provider", return_value=provider):
                transcript = transcriber.process_audio(
                    "empty-cache",
                    [{"local_path": str(source), "ext": ".mp3"}],
                )

        self.assertEqual(transcript[0]["text"], "hello")
        provider.transcribe.assert_called_once()

    def test_all_empty_chunks_fail_and_remove_completed_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            transcriber = AudioTranscriber(
                data_dir,
                self.mock_smil_extractor,
                self.mock_polisher,
            )
            source = data_dir / "source.mp3"
            source.write_bytes(b"audio")
            cache_dir = data_dir / "audio_cache" / "silent-book"
            normalized = cache_dir / "part_000.wav"
            provider = MagicMock()
            provider.transcribe.return_value = []
            transcriber.normalize_audio_to_wav = MagicMock(
                side_effect=lambda _path: normalized
            )
            transcriber.split_audio_file = MagicMock(return_value=[normalized])
            transcriber.get_audio_duration = MagicMock(return_value=10.0)

            with patch("utils.transcriber.get_transcription_provider", return_value=provider):
                with self.assertRaisesRegex(
                    ValueError, "Transcription completed without any segments"
                ):
                    transcriber.process_audio(
                        "silent-book",
                        [{"local_path": str(source), "ext": ".mp3"}],
                    )

            self.assertFalse((cache_dir / "_progress.json").exists())

if __name__ == '__main__':
    # unittest.main() # Avoid args issue in some environments?
    # Just standard main is fine for pytest discovery
    pass
