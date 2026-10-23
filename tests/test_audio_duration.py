"""probe_duration_seconds reads an audio file's length with ffprobe and never blocks or raises."""

import subprocess
from unittest.mock import MagicMock, patch

from src.utils.audio_duration import FFPROBE_TIMEOUT_SECONDS, probe_duration_seconds


def test_reads_the_length_ffprobe_reports():
    result = MagicMock(stdout="4549.62\n")
    with patch("src.utils.audio_duration.subprocess.run", return_value=result) as run:
        assert probe_duration_seconds("book.m4a") == 4549.62
    assert run.call_args.kwargs["timeout"] == FFPROBE_TIMEOUT_SECONDS


def test_a_stuck_ffprobe_gives_no_length():
    with patch("src.utils.audio_duration.subprocess.run",
               side_effect=subprocess.TimeoutExpired("ffprobe", FFPROBE_TIMEOUT_SECONDS)):
        assert probe_duration_seconds("book.m4a") is None


def test_an_unreadable_file_gives_no_length():
    with patch("src.utils.audio_duration.subprocess.run",
               side_effect=subprocess.CalledProcessError(1, "ffprobe")):
        assert probe_duration_seconds("book.m4a") is None
