"""Exact equality for ordered audio tracks."""

import hashlib
from pathlib import Path

def ordered_audio_matches(left, right):
    """Conservatively verify identical audio tracks in provider playback order."""
    if not left or len(left) != len(right):
        return False
    try:
        for a, b in zip(map(Path, left), map(Path, right)):
            if not a.is_file() or not b.is_file() or a.stat().st_size != b.stat().st_size:
                return False
            if a.samefile(b):
                continue
            with a.open('rb') as first, b.open('rb') as second:
                if hashlib.file_digest(first, 'sha256').digest() != hashlib.file_digest(second, 'sha256').digest():
                    return False
        return True
    except (OSError, TypeError, ValueError):
        return False
