"""Per-reader uploaded pictures (shelf trinkets today; later uploads reuse the same store).

Files live in ``DATA_DIR/uploads/<user id>/<purpose>/<random>.<ext>``, never under
``/static`` (static URLs are cached immutable for a year). Only SVG, PNG and WebP are
kept, judged by content rather than file name, and they are only ever shown as images.
"""

import re
import secrets
from pathlib import Path
from typing import Optional

from lxml import etree

UPLOAD_NAME = re.compile(r"[0-9a-f]{12}\.(?:svg|png|webp)")
MEDIA_TYPES = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp"}
TRINKETS = "trinkets"
TRINKET_MAX_BYTES = 512 * 1024
TRINKET_MAX_COUNT = 12


class UploadRejected(ValueError):
    """An upload that was refused; ``reason`` is a short stable code the page explains."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def image_kind(data: bytes) -> Optional[str]:
    """Return ``svg``, ``png`` or ``webp`` for a picture we accept, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    # No entity expansion, DTD loading or network: the file is parsed only to prove it is an SVG.
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    return "svg" if isinstance(root.tag, str) and etree.QName(root).localname == "svg" else None


def upload_dir(data_dir: Path, user_id: int, purpose: str) -> Path:
    """The folder holding one reader's uploads for one purpose."""
    return Path(data_dir) / "uploads" / str(int(user_id)) / purpose


def list_uploads(data_dir: Path, user_id: int, purpose: str) -> list[str]:
    """A reader's upload names, oldest first."""
    folder = upload_dir(data_dir, user_id, purpose)
    if not folder.is_dir():
        return []
    files = [path for path in folder.iterdir() if UPLOAD_NAME.fullmatch(path.name) and path.is_file()]
    return [path.name for path in sorted(files, key=lambda path: (path.stat().st_mtime, path.name))]


def save_upload(data_dir: Path, user_id: int, purpose: str, data: bytes, max_bytes: int, max_count: int) -> str:
    """Store one picture and return its name, or raise UploadRejected."""
    if not data:
        raise UploadRejected("empty")
    if len(data) > max_bytes:
        raise UploadRejected("too-big")
    kind = image_kind(data)
    if kind is None:
        raise UploadRejected("not-an-image")
    if len(list_uploads(data_dir, user_id, purpose)) >= max_count:
        raise UploadRejected("full")
    folder = upload_dir(data_dir, user_id, purpose)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(6)}.{kind}"
    (folder / name).write_bytes(data)
    return name


def upload_file(data_dir: Path, user_id: int, purpose: str, name: str) -> Optional[Path]:
    """The stored file for ``name`` in this reader's folder, or None."""
    if not UPLOAD_NAME.fullmatch(name or ""):
        return None
    path = upload_dir(data_dir, user_id, purpose) / name
    return path if path.is_file() else None


def delete_upload(data_dir: Path, user_id: int, purpose: str, name: str) -> bool:
    """Remove one of this reader's uploads; False when there was nothing to remove."""
    path = upload_file(data_dir, user_id, purpose, name)
    if path is None:
        return False
    path.unlink()
    return True
