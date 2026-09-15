"""Shared helpers that publish downloaded and copied files atomically.

Every writer here stages its payload into a unique hidden sibling of the
destination and only replaces the destination once the bytes have been received
and validated. An interrupted transfer therefore leaves the previous file intact
and never drops reusable-looking bytes into a cache directory.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Optional, Union
from uuid import uuid4

PathLike = Union[str, os.PathLike]

# Staged names are hidden and suffixed so a file left behind by a killed process
# can never match a reusable cache name.
_STAGING_SUFFIX = ".part"
_MAX_PREFIX_LEN = 80


class IncompleteTransferError(ValueError):
    """A transfer was rejected before the destination was replaced.

    Subclasses ``ValueError`` so callers that already catch ``ValueError`` keep
    working; ``actual_size``/``expected_size`` let them log exact byte counts.
    """

    def __init__(
        self,
        message: str,
        *,
        actual_size: int = 0,
        expected_size: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.actual_size = actual_size
        self.expected_size = expected_size


def body_was_decoded(response: Any) -> bool:
    """True when requests transparently decoded the body it handed back.

    A decoded body is longer than the Content-Length the server reported, so no
    size derived from the wire may be compared against what lands on disk.
    """
    try:
        encoding = str(response.headers.get("Content-Encoding") or "").strip().lower()
    except Exception:
        return False
    return bool(encoding) and encoding != "identity"


def response_declares_size(response: Any) -> Optional[int]:
    """Return a comparable Content-Length for ``response``, else ``None``."""
    if body_was_decoded(response):
        return None
    try:
        declared = str(response.headers.get("Content-Length") or "").strip()
    except Exception:
        return None
    return int(declared) if declared.isdigit() else None


def _staging_file(destination: Path) -> Any:
    """Open a hidden, uniquely named staging file beside ``destination``."""
    return NamedTemporaryFile(
        dir=str(destination.parent),
        prefix=f".{destination.name[:_MAX_PREFIX_LEN]}.",
        suffix=_STAGING_SUFFIX,
        delete=False,
    )


def _validate_size(
    destination: Path,
    actual_size: int,
    expected_size: Optional[int],
    min_size: int,
) -> None:
    """Reject an empty, undersized, or short payload before it is published."""
    if actual_size <= min_size:
        if actual_size <= 0:
            raise IncompleteTransferError(
                f"empty payload for {destination}",
                actual_size=actual_size,
                expected_size=expected_size,
            )
        raise IncompleteTransferError(
            f"got {actual_size} bytes, expected more than {min_size} for {destination}",
            actual_size=actual_size,
            expected_size=expected_size,
        )
    if expected_size is not None and expected_size > 0 and actual_size != expected_size:
        raise IncompleteTransferError(
            f"got {actual_size} bytes, expected {expected_size} for {destination}",
            actual_size=actual_size,
            expected_size=expected_size,
        )


def _discard(temporary_path: Optional[Path]) -> None:
    """Remove a staging file, never masking the failure that led here."""
    if temporary_path is None:
        return
    try:
        temporary_path.unlink(missing_ok=True)
    except Exception:
        pass


def stream_response_to_path(
    response: Any,
    output_path: PathLike,
    *,
    expected_size: Optional[int] = None,
    min_size: int = 0,
    chunk_size: int = 8192,
    validator: Optional[Callable[[Path], None]] = None,
    on_chunk: Optional[Callable[[], None]] = None,
) -> bool:
    """Stream ``response`` into a staging file and publish it atomically.

    ``expected_size`` is ignored when the body was transparently decoded, since
    Content-Length then describes the compressed wire length. ``validator`` runs
    against the staged file before publication and rejects it by raising. Raises
    ``on_chunk`` is called for each received chunk so a caller can abort a long
    transfer by raising. Raises ``IncompleteTransferError`` for an empty,
    undersized, short, or partial body; on any failure the destination is left
    untouched and the staging file removed.
    """
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None

    # A 206 answers a Range request; we asked for the whole file, so a partial
    # body here is a truncation the size check alone might not catch.
    if getattr(response, "status_code", None) == 206:
        raise IncompleteTransferError(
            f"server returned partial content (HTTP 206) for {destination}"
        )

    if body_was_decoded(response):
        expected_size = None

    try:
        with _staging_file(destination) as handle:
            temporary_path = Path(handle.name)
            for chunk in response.iter_content(chunk_size=chunk_size):
                if on_chunk is not None:
                    on_chunk()
                if chunk:
                    handle.write(chunk)

        _validate_size(destination, temporary_path.stat().st_size, expected_size, min_size)
        if validator is not None:
            validator(temporary_path)
        temporary_path.replace(destination)
        return True
    except BaseException:
        _discard(temporary_path)
        raise


def copy_file_to_path(
    source_path: PathLike,
    output_path: PathLike,
    *,
    min_size: int = 0,
    validator: Optional[Callable[[Path], None]] = None,
) -> bool:
    """Copy a local file into place atomically, never touching the source.

    The source is stat'd before and after the copy so a file still being written
    is rejected instead of published. ``validator`` runs against the staged copy
    before publication and rejects it by raising. Raises
    ``IncompleteTransferError`` when the copy is short, undersized, or the source
    changed while it was read.
    """
    source = Path(source_path)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None

    before = source.stat()
    try:
        with _staging_file(destination) as handle:
            temporary_path = Path(handle.name)
            with open(source, "rb") as reader:
                shutil.copyfileobj(reader, handle)

        after = source.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise IncompleteTransferError(
                f"source {source} changed while being copied to {destination}",
                actual_size=temporary_path.stat().st_size,
                expected_size=before.st_size,
            )

        _validate_size(destination, temporary_path.stat().st_size, before.st_size, min_size)
        if validator is not None:
            validator(temporary_path)
        temporary_path.replace(destination)
        return True
    except BaseException:
        _discard(temporary_path)
        raise


def hardlink_file_to_path(source_path: PathLike, output_path: PathLike) -> bool:
    """Hardlink ``source_path`` into place atomically, never touching the source.

    The link is created under a staging name and then moved over the destination,
    so a failed link leaves any previously staged file in place. Raises whatever
    ``os.link`` raises when the filesystem cannot satisfy the request.
    """
    source = Path(source_path)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.parent / (
        f".{destination.name[:_MAX_PREFIX_LEN]}.{uuid4().hex}.link"
    )
    try:
        os.link(source, temporary_path)
        temporary_path.replace(destination)
        return True
    except BaseException:
        _discard(temporary_path)
        raise
