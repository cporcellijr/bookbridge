import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from utils.transcription_providers import (
    LocalWhisperProvider,
    DeepgramProvider,
    WhisperCppServerProvider,
    get_transcription_provider,
)

class TestLocalWhisperProvider(unittest.TestCase):
    
    @patch.dict(os.environ, {}, clear=True)
    def test_default_init(self):
        """Test default initialization with no env vars."""
        provider = LocalWhisperProvider()
        self.assertEqual(provider.model_size, "base")
        self.assertEqual(provider.whisper_device, "auto")
        self.assertEqual(provider.whisper_compute_type, "auto")
        self.assertIn("LocalWhisper", provider.get_name())

    @staticmethod
    def _fake_cuda_env(libs_bundled: bool, device_count: int = 0):
        """Simulate CUDA libs and visible GPU (both required for CUDA to work in the container)"""
        mock_ct2 = MagicMock()
        mock_ct2.get_cuda_device_count.return_value = device_count
        return (
            patch("utils.transcription_providers.importlib.util.find_spec",
                  return_value=MagicMock() if libs_bundled else None),
            patch.dict(sys.modules, {'ctranslate2': mock_ct2}),
        )

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_auto_gpu(self, mock_logger):
        """CUDA image on a host with a GPU: auto picks cuda."""
        provider = LocalWhisperProvider()
        find_spec, ct2 = self._fake_cuda_env(libs_bundled=True, device_count=1)

        with find_spec, ct2:
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cuda")
        self.assertEqual(compute_type, "float16")  # Default for GPU in auto mode

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_auto_cpu_no_libs(self, mock_logger):
        """CPU image on a GPU host: no bundled CUDA libs, so stay on CPU."""
        provider = LocalWhisperProvider()
        find_spec, ct2 = self._fake_cuda_env(libs_bundled=False, device_count=1)

        with find_spec, ct2:
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")  # Default for CPU in auto mode

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_cpu_image_without_nvidia_package(self, mock_logger):
        """Regression for #355: on the non-CUDA image the parent 'nvidia' package
        does not exist, so find_spec('nvidia.cudnn') RAISES ModuleNotFoundError
        instead of returning None. auto must fall back to CPU, not crash."""
        provider = LocalWhisperProvider()

        with patch(
            "utils.transcription_providers.importlib.util.find_spec",
            side_effect=ModuleNotFoundError("No module named 'nvidia'"),
        ):
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")
        logged = " ".join(str(c) for c in mock_logger.info.call_args_list)
        self.assertIn("CUDA libraries not bundled", logged)

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_survives_find_spec_valueerror(self, mock_logger):
        """find_spec can also raise ValueError (parent with __spec__ = None);
        treat it the same as 'CUDA libraries absent'."""
        provider = LocalWhisperProvider()

        with patch(
            "utils.transcription_providers.importlib.util.find_spec",
            side_effect=ValueError("nvidia.__spec__ is None"),
        ):
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")

    @patch("utils.transcription_providers.logger")
    def test_get_device_config_auto_cpu_no_gpu(self, mock_logger):
        """CUDA image with no GPU passed through to the container: stay on CPU."""
        provider = LocalWhisperProvider()
        find_spec, ct2 = self._fake_cuda_env(libs_bundled=True, device_count=0)

        with find_spec, ct2:
            device, compute_type = provider._get_device_config()

        self.assertEqual(device, "cpu")
        self.assertEqual(compute_type, "int8")


    @patch("utils.transcription_providers.logger")
    def test_explicit_config(self, mock_logger):
        """Test that explicit environment variables override auto detection."""
        with patch.dict(os.environ, {
            "WHISPER_DEVICE": "cpu", 
            "WHISPER_COMPUTE_TYPE": "int8"
        }):
            provider = LocalWhisperProvider()
            device, compute_type = provider._get_device_config()
            
            self.assertEqual(device, "cpu")
            self.assertEqual(compute_type, "int8")

    @patch("faster_whisper.WhisperModel")
    @patch("utils.transcription_providers.logger")
    @patch.dict(os.environ, {"WHISPER_MODEL": "base", "WHISPER_DEVICE": "auto"}, clear=True)
    def test_model_initialization_gpu(self, mock_logger, mock_whisper_model):
        """Test that WhisperModel is initialized with correct GPU params."""
        provider = LocalWhisperProvider()
        
        # Force GPU config via mock
        with patch.object(provider, '_get_device_config', return_value=('cuda', 'float16')):
            provider._get_model()
            expected_download_root = str(Path(os.environ.get("DATA_DIR", "/data")) / "models")
            
            mock_whisper_model.assert_called_once_with(
                'base', 
                download_root=expected_download_root,
                device='cuda', 
                compute_type='float16'
            )

    @patch("faster_whisper.WhisperModel")
    @patch("utils.transcription_providers.logger")
    @patch.dict(os.environ, {"WHISPER_MODEL": "base", "WHISPER_DEVICE": "auto"}, clear=True)
    def test_transcribe_with_word_timestamps(self, mock_logger, mock_whisper_model):
        """Test that transcribe calls model with word_timestamps=True and retains words."""
        provider = LocalWhisperProvider()

        # Mock the model and its transcribe method
        mock_model_instance = mock_whisper_model.return_value

        # Create mock segments with words
        mock_segment1 = MagicMock()
        mock_segment1.start = 0.0
        mock_segment1.end = 2.0
        mock_segment1.text = "Hello world"

        mock_word1 = MagicMock()
        mock_word1.word = "Hello"
        mock_word1.start = 0.0
        mock_word1.end = 0.5

        mock_word2 = MagicMock()
        mock_word2.word = " world"
        mock_word2.start = 0.5
        mock_word2.end = 2.0

        mock_segment1.words = [mock_word1, mock_word2]

        mock_segment2 = MagicMock()
        mock_segment2.start = 2.5
        mock_segment2.end = 4.0
        mock_segment2.text = "How are you"
        mock_segment2.words = None  # Test handling of None words

        mock_model_instance.transcribe.return_value = ([mock_segment1, mock_segment2], MagicMock())

        with patch.object(provider, '_get_device_config', return_value=('cpu', 'int8')):
            segments = provider.transcribe(Path("test.wav"))

        # Verify word_timestamps=True was passed
        mock_model_instance.transcribe.assert_called_once()
        _, kwargs = mock_model_instance.transcribe.call_args
        self.assertTrue(kwargs.get('word_timestamps'))

        # Verify segments and words
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]['text'], "Hello world")
        self.assertEqual(segments[0]['start'], 0.0)
        self.assertEqual(segments[0]['end'], 2.0)
        self.assertIn('words', segments[0])
        self.assertEqual(len(segments[0]['words']), 2)
        self.assertEqual(segments[0]['words'][0], {"word": "Hello", "start": 0.0, "end": 0.5})
        self.assertEqual(segments[0]['words'][1], {"word": " world", "start": 0.5, "end": 2.0})

        # Second segment has no words
        self.assertEqual(segments[1]['text'], "How are you")
        self.assertNotIn('words', segments[1])

class TestDeepgramProvider(unittest.TestCase):
    
    def test_init_without_key(self):
        """Test initialization works but transcribe fails without key."""
        with patch.dict(os.environ, {}, clear=True):
            provider = DeepgramProvider()
            self.assertEqual(provider.api_key, "")
            
            with self.assertRaises(ValueError):
                provider.transcribe(Path("dummy.wav"))

    def test_init_with_key(self):
        """Test initialization with key."""
        with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_key", "DEEPGRAM_MODEL": "nova-3"}):
            provider = DeepgramProvider()
            self.assertEqual(provider.api_key, "test_key")
            self.assertEqual(provider.model, "nova-3")
            self.assertIn("nova-3", provider.get_name())

    def test_transcribe(self):
        """Test transcribe calls Deepgram API correctly with new SDK."""
        # Create a mock for the deepgram module
        mock_deepgram = MagicMock()
        mock_client_cls = MagicMock()
        mock_deepgram.DeepgramClient = mock_client_cls
        
        # Patch sys.modules to include deepgram
        with patch.dict(sys.modules, {'deepgram': mock_deepgram}):
            with patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test_key"}):
                provider = DeepgramProvider()
                
                # Mock the client chain: client.listen.v1.media.transcribe_file
                mock_client = mock_client_cls.return_value
                mock_transcribe = mock_client.listen.v1.media.transcribe_file
                
                # Mock response structure
                mock_response = MagicMock()
                # Setup utterances structure
                mock_utterance = MagicMock()
                mock_utterance.start = 0.5
                mock_utterance.end = 2.5
                mock_utterance.transcript = "Hello world"
                
                mock_response.results.utterances = [mock_utterance]
                mock_transcribe.return_value = mock_response
                
                # Create a dummy file to read
                with patch("builtins.open", new_callable=unittest.mock.mock_open, read_data=b"audio_data"):
                    segments = provider.transcribe(Path("test.mp3"))
                
                # Verify client init
                mock_client_cls.assert_called_once_with(api_key="test_key")
                
                # Verify transcribe call args - ensure NO timeout and correct model
                mock_transcribe.assert_called_once()
                _, kwargs = mock_transcribe.call_args
                self.assertEqual(kwargs['model'], 'nova-2')
                self.assertEqual(kwargs['smart_format'], True)
                self.assertNotIn('timeout', kwargs) # IMPORTANT: timeout should NOT be passed
                
                # Verify result parsing
                self.assertEqual(len(segments), 1)
                self.assertEqual(segments[0]['text'], "Hello world")
                self.assertEqual(segments[0]['start'], 0.5)
                self.assertEqual(segments[0]['end'], 2.5)

class TestWhisperCppServerProvider(unittest.TestCase):

    def test_init_without_url_raises(self):
        """Missing WHISPER_CPP_URL must fail loudly."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                WhisperCppServerProvider()

    def test_defaults(self):
        """Raw upload is off by default and does not advertise supports_raw_audio."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()
            self.assertFalse(provider.send_original)
            self.assertFalse(provider.supports_raw_audio)
            self.assertEqual(provider.timeout, 600)

    def test_send_original_enables_raw_audio(self):
        """WHISPER_CPP_SEND_ORIGINAL=true makes the pipeline skip WAV normalization."""
        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_SEND_ORIGINAL": "true",
        }, clear=True):
            provider = WhisperCppServerProvider()
            self.assertTrue(provider.send_original)
            self.assertTrue(provider.supports_raw_audio)

    def test_transcribe_local_file_parses_segments(self):
        """Local file upload posts verbose_json and parses segment timestamps."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "segments": [{"start": 1.0, "end": 2.5, "text": " hello world "}]
        }
        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(segments, [{"start": 1.0, "end": 2.5, "text": "hello world"}])
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["response_format"], "verbose_json")
        self.assertEqual(kwargs["timeout"], 600)

    def test_transcribe_url_source_downloads_then_uploads(self):
        """A stream URL source is buffered to a temp file and uploaded."""
        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_SEND_ORIGINAL": "true",
        }, clear=True):
            provider = WhisperCppServerProvider()

        mock_get_resp = MagicMock()
        mock_get_resp.__enter__ = MagicMock(return_value=mock_get_resp)
        mock_get_resp.__exit__ = MagicMock(return_value=False)
        mock_get_resp.iter_content.return_value = [b"audio-bytes"]

        mock_post_resp = MagicMock()
        mock_post_resp.json.return_value = {
            "segments": [{"start": 0.0, "end": 3.0, "text": "streamed"}]
        }

        with patch("requests.get", return_value=mock_get_resp) as mock_get, \
             patch("requests.post", return_value=mock_post_resp) as mock_post:
            segments = provider.transcribe("http://abs/stream/part.m4b?token=abc")

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args[0][0], "http://abs/stream/part.m4b?token=abc")
        mock_post.assert_called_once()
        # Upload uses the source filename (query string stripped)
        upload_name = mock_post.call_args[1]["files"]["file"][0]
        self.assertEqual(upload_name, "part.m4b")
        self.assertEqual(segments, [{"start": 0.0, "end": 3.0, "text": "streamed"}])

    def test_text_only_response_warns_and_degrades(self):
        """Servers ignoring verbose_json still return a usable (untimed) segment."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "plain transcript"}
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(segments, [{"start": 0.0, "end": 0.0, "text": "plain transcript"}])

    def test_chunked_wav_upload_offsets_timestamps(self):
        """WHISPER_CPP_CHUNK_MINUTES splits WAVs and offsets returned timestamps."""
        import io
        import tempfile
        import wave

        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_CHUNK_MINUTES": "1",
        }, clear=True):
            provider = WhisperCppServerProvider()

        # 90 seconds of silence at 16kHz mono -> two chunks (60s + 30s)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 16000 * 90)
            wav_path = Path(tmp.name)

        durations = []

        def fake_post(url, files=None, data=None, timeout=None):
            buf = files["file"][1]
            with wave.open(io.BytesIO(buf.read()), "rb") as wf:
                dur = wf.getnframes() / wf.getframerate()
            durations.append(dur)
            resp = MagicMock()
            resp.json.return_value = {
                "segments": [{"start": 0.0, "end": dur, "text": f"part {len(durations)}"}]
            }
            return resp

        try:
            with patch("requests.post", side_effect=fake_post):
                segments = provider.transcribe(wav_path)
        finally:
            wav_path.unlink()

        self.assertEqual(durations, [60.0, 30.0])
        self.assertEqual(segments, [
            {"start": 0.0, "end": 60.0, "text": "part 1"},
            {"start": 60.0, "end": 90.0, "text": "part 2"},
        ])

    def test_factory_returns_whispercpp(self):
        """Factory selects WhisperCppServerProvider when configured."""
        with patch.dict(os.environ, {
            "TRANSCRIPTION_PROVIDER": "whispercpp",
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
        }, clear=True):
            provider = get_transcription_provider()
            self.assertIsInstance(provider, WhisperCppServerProvider)

    def test_post_requests_word_and_segment_granularity(self):
        """POST includes both timestamp_granularities and timestamp_granularities[] for word+segment."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"segments": [{"start": 0.0, "end": 1.0, "text": "test"}]}
        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            provider.transcribe(Path("chunk.wav"))

        _, kwargs = mock_post.call_args
        data = kwargs["data"]
        self.assertIn("timestamp_granularities", data)
        self.assertIn("timestamp_granularities[]", data)
        self.assertEqual(data["timestamp_granularities"], ["word", "segment"])
        self.assertEqual(data["timestamp_granularities[]"], ["word", "segment"])

    def test_nested_segment_words_retained(self):
        """Server returns words inside segments; they are validated and retained."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "segments": [{
                "start": 0.0,
                "end": 2.0,
                "text": "Hello world",
                "words": [
                    {"word": "Hello", "start": 0.0, "end": 0.5},
                    {"word": " world", "start": 0.5, "end": 2.0}
                ]
            }]
        }
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(len(segments), 1)
        self.assertIn("words", segments[0])
        self.assertEqual(len(segments[0]["words"]), 2)
        self.assertEqual(segments[0]["words"][0], {"word": "Hello", "start": 0.0, "end": 0.5})
        self.assertEqual(segments[0]["words"][1], {"word": " world", "start": 0.5, "end": 2.0})

    def test_top_level_words_associated_with_segments(self):
        """Top-level words array is associated with segments by start time."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "Hello world"},
                {"start": 2.5, "end": 4.0, "text": "How are you"}
            ],
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.5, "end": 2.0},
                {"word": "How", "start": 2.5, "end": 3.0},
                {"word": "are", "start": 3.0, "end": 3.5},
                {"word": "you", "start": 3.5, "end": 4.0}
            ]
        }
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(len(segments), 2)
        self.assertIn("words", segments[0])
        self.assertEqual(len(segments[0]["words"]), 2)
        self.assertEqual(segments[0]["words"][0]["word"], "Hello")
        self.assertEqual(segments[0]["words"][1]["word"], "world")
        self.assertIn("words", segments[1])
        self.assertEqual(len(segments[1]["words"]), 3)
        self.assertEqual(segments[1]["words"][0]["word"], "How")
        self.assertEqual(segments[1]["words"][1]["word"], "are")
        self.assertEqual(segments[1]["words"][2]["word"], "you")

    def test_words_only_response_creates_single_segment(self):
        """Words-only response (no segments) creates a single segment with joined text."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.5},
                {"word": "world", "start": 0.5, "end": 2.0}
            ],
            "text": "Hello world"
        }
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 2.0)
        self.assertEqual(segments[0]["text"], "Hello world")
        self.assertIn("words", segments[0])
        self.assertEqual(len(segments[0]["words"]), 2)

    def test_malformed_nested_words_falls_back_to_segment_only(self):
        """Malformed words in segments cause fallback to segment-only (no partial words attached)."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        # Missing 'word' field
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "segments": [{
                "start": 0.0,
                "end": 2.0,
                "text": "Hello world",
                "words": [{"start": 0.0, "end": 0.5}]  # missing word/text
            }]
        }
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(len(segments), 1)
        self.assertNotIn("words", segments[0])  # No words attached due to validation failure

    def test_malformed_top_level_words_falls_back_to_segment_only(self):
        """Malformed top-level words cause fallback to segment-only (no words attached)."""
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/v1/audio/transcriptions"}, clear=True):
            provider = WhisperCppServerProvider()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "segments": [{"start": 0.0, "end": 2.0, "text": "Hello world"}],
            "words": [{"word": "Hello", "start": "invalid", "end": 0.5}]  # invalid start
        }
        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", unittest.mock.mock_open(read_data=b"wav")):
            segments = provider.transcribe(Path("chunk.wav"))

        self.assertEqual(len(segments), 1)
        self.assertNotIn("words", segments[0])  # No words attached due to validation failure

    def test_invalid_word_shapes_and_nonfinite_times_fall_back(self):
        with patch.dict(os.environ, {"WHISPER_CPP_URL": "http://x/transcriptions"}):
            provider = WhisperCppServerProvider()
        invalid = [
            "not a list", [None], ["not a record"],
            [{"word": "hello", "start": 0, "end": float('inf')}],
            [{"word": "hello", "start": float('nan'), "end": 1}],
            [{"word": "hello", "start": 2, "end": 3},
             {"word": "world", "start": 1, "end": 2}],
        ]
        for words in invalid:
            with self.subTest(words=words):
                response = MagicMock()
                response.json.return_value = {
                    "segments": [{"start": 0, "end": 3, "text": "hello world", "words": words}],
                }
                with patch('requests.post', return_value=response):
                    self.assertEqual(provider._post(None, 'test.wav', 'audio/wav'),
                                     [{"start": 0.0, "end": 3.0, "text": "hello world"}])

    def test_chunked_wav_offsets_nested_word_timestamps(self):
        """Second WAV chunk offsets nested word start/end along with segment timestamps."""
        import io
        import tempfile
        import wave

        with patch.dict(os.environ, {
            "WHISPER_CPP_URL": "http://x/v1/audio/transcriptions",
            "WHISPER_CPP_CHUNK_MINUTES": "1",
        }, clear=True):
            provider = WhisperCppServerProvider()

        # 90 seconds of silence at 16kHz mono -> two chunks (60s + 30s)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(b"\x00\x00" * 16000 * 90)
            wav_path = Path(tmp.name)

        call_count = [0]

        def fake_post(url, files=None, data=None, timeout=None):
            call_count[0] += 1
            buf = files["file"][1]
            with wave.open(io.BytesIO(buf.read()), "rb") as wf:
                dur = wf.getnframes() / wf.getframerate()
            resp = MagicMock()
            if call_count[0] == 1:
                # First chunk: segment with nested words
                resp.json.return_value = {
                    "segments": [{
                        "start": 0.0,
                        "end": dur,
                        "text": "part 1",
                        "words": [
                            {"word": "part", "start": 0.0, "end": 0.5},
                            {"word": "1", "start": 0.5, "end": dur}
                        ]
                    }]
                }
            else:
                # Second chunk: segment with nested words
                resp.json.return_value = {
                    "segments": [{
                        "start": 0.0,
                        "end": dur,
                        "text": "part 2",
                        "words": [
                            {"word": "part", "start": 0.0, "end": 0.5},
                            {"word": "2", "start": 0.5, "end": dur}
                        ]
                    }]
                }
            return resp

        try:
            with patch("requests.post", side_effect=fake_post):
                segments = provider.transcribe(wav_path)
        finally:
            wav_path.unlink()

        self.assertEqual(len(segments), 2)
        # First chunk: words at 0.0-0.5, 0.5-60.0
        self.assertIn("words", segments[0])
        self.assertEqual(segments[0]["words"][0], {"word": "part", "start": 0.0, "end": 0.5})
        self.assertEqual(segments[0]["words"][1], {"word": "1", "start": 0.5, "end": 60.0})
        # Second chunk: words offset by 60s -> 60.0-60.5, 60.5-90.0
        self.assertIn("words", segments[1])
        self.assertEqual(segments[1]["words"][0], {"word": "part", "start": 60.0, "end": 60.5})
        self.assertEqual(segments[1]["words"][1], {"word": "2", "start": 60.5, "end": 90.0})


if __name__ == '__main__':
    unittest.main()
