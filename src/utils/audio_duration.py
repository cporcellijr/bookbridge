"""An audio file's length via ``ffprobe``.

A plain subprocess call rather than ``Transcriber.get_audio_duration``, whose module
pulls in the transcription stack's heavier dependencies.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

# ffprobe reads only the container header, so a local file answers in well under a
# second. A stuck process would otherwise block whatever is waiting on the length.
FFPROBE_TIMEOUT_SECONDS = 60


def probe_duration_seconds(path: Union[str, Path]) -> Optional[float]:
    """The duration, in seconds, of an audio file via ``ffprobe``.

    Returns ``None`` -- never raises -- on any failure, so a caller can carry on
    without the length instead of failing the whole job.
    """
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
                                timeout=FFPROBE_TIMEOUT_SECONDS)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        logger.warning("Could not probe audio duration for '%s': %s", path, e, exc_info=True)
        return None
