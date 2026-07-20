# Transcription Providers
"""
Abstract interface and implementations for transcription services.
Supports local Whisper and cloud providers like Deepgram.
"""

import importlib.util
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TranscriptionSegment:
    """Represents a single transcription segment with timing."""
    def __init__(self, start: float, end: float, text: str):
        self.start = start
        self.end = end
        self.text = text
    
    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text
        }


class TranscriptionProvider(ABC):
    """Abstract base class for transcription providers."""
    
    @abstractmethod
    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        """
        Transcribe an audio file and return segments.
        
        Args:
            audio_path: Path to the audio file
            progress_callback: Optional callback for progress updates (0.0 to 1.0)
        
        Returns:
            List of dicts with 'start', 'end', 'text' keys
        """
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the provider name for logging."""
        pass


class LocalWhisperProvider(TranscriptionProvider):
    """Local Whisper transcription using faster-whisper."""
    
    def __init__(self):
        self.model_size = os.environ.get("WHISPER_MODEL", "base")
        self.whisper_device = os.environ.get("WHISPER_DEVICE", "auto").lower()
        self.whisper_compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "auto").lower()
        self._model = None
    
    def get_name(self) -> str:
        return f"LocalWhisper ({self.model_size})"
    
    def _detect_cuda(self) -> tuple[bool, str]:
        """
        Check whether CTranslate2 can actually run on CUDA here.
        This is dependent on the image that is used (only the image with the -cuda suffix has CUDA libraries)
        and whether the GPU is visible in the container (via deploy block in the compose file)
        """
        if importlib.util.find_spec("nvidia.cudnn") is None:
            return False, "CUDA libraries not bundled (use the -cuda image tag)"

        try:
            import ctranslate2 # transitive dependency by faster-whisper
            device_count = ctranslate2.get_cuda_device_count()
        except Exception as e:
            return False, f"CUDA probe failed: {e}"

        if device_count < 1:
            return False, "no GPU visible to the container"

        return True, f"{device_count} CUDA device(s) available"

    def _get_device_config(self) -> tuple[str, str]:
        """Determine device and compute type with auto-detection."""
        device = self.whisper_device
        compute_type = self.whisper_compute_type
        
        if device == 'auto':
            cuda_ok, reason = self._detect_cuda()
            if cuda_ok:
                device = 'cuda'
                logger.info(f"🎮 CUDA enabled: {reason}")
            else:
                device = 'cpu'
                logger.info(f"💻 Using CPU: {reason}")
        
        if compute_type == 'auto':
            compute_type = 'float16' if device == 'cuda' else 'int8'
        
        return device, compute_type
    
    def _get_model(self):
        """Lazy-load the Whisper model."""
        if self._model is None:
            from faster_whisper import WhisperModel
            device, compute_type = self._get_device_config()
            logger.info(f"⚙️ Loading Whisper: model={self.model_size}, device={device}, compute_type={compute_type}")
            
            model_kwargs = {'device': device, 'compute_type': compute_type}
            if device == 'cpu':
                model_kwargs['cpu_threads'] = 4
            
            self._model = WhisperModel(
                self.model_size,
                download_root=str(Path(os.environ.get("DATA_DIR", "/data")) / "models"),
                **model_kwargs,
            )
        return self._model
    
    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        """Transcribe using local Whisper model."""
        model = self._get_model()
        segments_out = []
        
        logger.info(f"🧠 Transcribing with {self.get_name()}: {audio_path.name}")
        segments, info = model.transcribe(str(audio_path), beam_size=1, best_of=1)
        
        for segment in segments:
            segments_out.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip()
            })
        
        logger.info(f"✅ Transcription complete: {len(segments_out)} segments")
        return segments_out


class DeepgramProvider(TranscriptionProvider):
    """Cloud transcription using Deepgram API."""
    
    def __init__(self):
        self.api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        self.model = os.environ.get("DEEPGRAM_MODEL", "nova-2")
    
    def get_name(self) -> str:
        return f"Deepgram ({self.model})"
    
    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        """Transcribe using Deepgram API."""
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY not configured")
        
        # Import from the current SDK location.
        from deepgram import DeepgramClient
        
        logger.info(f"☁️ Transcribing with {self.get_name()}: {audio_path.name}")
        
        # Initialize the client with the API key.
        client = DeepgramClient(api_key=self.api_key)
        
        with open(audio_path, "rb") as f:
            buffer_data = f.read()
        
        # Use the API's supported transcription arguments.
        response = client.listen.v1.media.transcribe_file(
            request=buffer_data,
            model=self.model,
            smart_format=True,
            utterances=True,
            punctuate=True,
            request_options={
               "timeout_in_seconds": 600  # Set to 10 minutes (default is 60s)
            }
        )
        
        segments_out = []
        
        # Parse Deepgram response
        if hasattr(response, 'results') and response.results:
            # Try utterances first (preferred for sentence-level segments)
            if hasattr(response.results, 'utterances') and response.results.utterances:
                for utterance in response.results.utterances:
                    segments_out.append({
                        "start": utterance.start,
                        "end": utterance.end,
                        "text": utterance.transcript.strip()
                    })
            # Fallback to channels/words
            elif hasattr(response.results, 'channels') and response.results.channels:
                for channel in response.results.channels:
                    for alternative in channel.alternatives:
                        if hasattr(alternative, 'words') and alternative.words:
                            current_segment = {"start": 0, "end": 0, "text": ""}
                            for word in alternative.words:
                                if not current_segment["text"]:
                                    current_segment["start"] = word.start
                                current_segment["end"] = word.end
                                word_text = getattr(word, 'punctuated_word', word.word) or word.word
                                current_segment["text"] += word_text + " "
                                
                                if word_text and word_text[-1] in '.!?':
                                    current_segment["text"] = current_segment["text"].strip()
                                    segments_out.append(current_segment)
                                    current_segment = {"start": 0, "end": 0, "text": ""}
                            
                            if current_segment["text"]:
                                current_segment["text"] = current_segment["text"].strip()
                                segments_out.append(current_segment)
                        elif hasattr(alternative, 'transcript') and alternative.transcript:
                            segments_out.append({
                                "start": 0,
                                "end": 0,
                                "text": alternative.transcript.strip()
                            })
        
        logger.info(f"✅ Deepgram transcription complete: {len(segments_out)} segments")
        return segments_out

class WhisperCppServerProvider(TranscriptionProvider):
    """Transcription via whisper.cpp HTTP server (ggml-org)."""

    def __init__(self):
        url = os.environ.get("WHISPER_CPP_URL", "").strip()
        if not url:
            raise ValueError(
                "WHISPER_CPP_URL is not configured. Set it in Settings → Advanced Options."
            )
        self.server_url = url
        self.model = os.environ.get("WHISPER_MODEL", "small")

    def get_name(self) -> str:
        return f"Whisper.cpp (server) - {self.model}"

    def transcribe(self, audio_path: Path, progress_callback=None) -> list[dict]:
        import requests

        logger.info(f"🌐 Transcribing with {self.get_name()}: {audio_path.name}")

        with open(audio_path, "rb") as f:
            files = {
                "file": (audio_path.name, f, "audio/wav")
            }
            data = {"model": self.model, "response_format": "verbose_json"}

            response = requests.post(
                self.server_url,
                files=files,
                data=data,
                timeout=600
            )

        if not response.ok:
            # Surface the server's error body — whisper.cpp / llama-swap explains
            # *why* here (e.g. unknown model id, failed to decode audio). Without
            # this the caller only sees a bare "HTTP 400" and cannot diagnose it.
            body = (response.text or "").strip()
            if len(body) > 500:
                body = body[:500] + "…"
            logger.error(
                f"❌ whisper.cpp server returned HTTP {response.status_code} from "
                f"{self.server_url} (model={self.model}). Response: {body or '<empty body>'}"
            )
            response.raise_for_status()

        result = response.json()

        segments_out = []
        for seg in result.get("segments", []):
            segments_out.append({
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": seg["text"].strip()
            })

        logger.info(f"✅ whisper.cpp transcription complete: {len(segments_out)} segments")
        return segments_out


def get_transcription_provider() -> TranscriptionProvider:
    """Factory function to get the configured transcription provider."""
    provider_name = os.environ.get("TRANSCRIPTION_PROVIDER", "local").lower()
    
    if provider_name == "deepgram":
        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            logger.warning("⚠️ Deepgram selected but no API key configured, falling back to local")
            return LocalWhisperProvider()
        return DeepgramProvider()
    elif provider_name == "whispercpp":
        url = os.environ.get("WHISPER_CPP_URL", "").strip()
        if not url:
            logger.warning("⚠️ Whisper.cpp selected but no URL configured, falling back to local")
            return LocalWhisperProvider()
        return WhisperCppServerProvider()
    else:
        return LocalWhisperProvider()
