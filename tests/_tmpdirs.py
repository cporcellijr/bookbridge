"""Per-process scratch folders for test modules that need a real DATA_DIR."""

import shutil
import tempfile
import time
from pathlib import Path

_ROOT = Path(tempfile.gettempdir()) / "bookbridge-test-dirs"
_STALE_SECONDS = 3600


def process_test_dir(name: str) -> str:
    """Return a new folder unique to this process.

    A shared fixed path collides across xdist workers and overlapping runs. Folders
    cannot be removed at exit on Windows (the module's SQLite file is still open), so
    each call clears leftovers from runs that finished over an hour ago instead.
    """
    _ROOT.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _STALE_SECONDS
    for old in _ROOT.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue
    return tempfile.mkdtemp(prefix=f"{name}_", dir=_ROOT)
