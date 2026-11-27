"""Fingerprint local audio without staging or buffering the complete file."""

import hashlib
import os


def fingerprint_local_audio(path):
    with open(path, 'rb') as audio:
        before = os.fstat(audio.fileno())
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: audio.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(audio.fileno())
    if not size or size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise OSError('Local audio is empty or changed during verification')
    return size, digest.digest()
