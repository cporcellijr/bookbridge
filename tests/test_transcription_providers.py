import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from utils.transcription_providers import LocalWhisperProvider, DeepgramProvider, get_transcription_provider

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

if __name__ == '__main__':
    unittest.main()
