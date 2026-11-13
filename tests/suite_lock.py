"""Cross-process lock for pytest controllers."""

import errno
import os
from pathlib import Path
from typing import BinaryIO


class SuiteLockBusy(Exception):
    """Raised when another pytest controller owns the suite lock."""


class SuiteLock:
    """Hold an OS file lock until the handle is closed."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        """Acquire the lock without waiting for its current owner."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        self._file.seek(0, os.SEEK_END)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            if (
                exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
                or getattr(exc, "winerror", None) in (33, 36)
            ):
                raise SuiteLockBusy(str(self.path)) from exc
            raise

    def close(self) -> None:
        """Release the lock by closing its file handle."""
        if self._file is not None:
            self._file.close()
            self._file = None
