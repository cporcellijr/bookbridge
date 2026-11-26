"""Identify playable media overlays and, conservatively, their audio clock."""

import hashlib
import posixpath
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException


def _resolve_member(base, href):
    ref = urlsplit(href or "")
    if ref.scheme or ref.netloc or not ref.path:
        raise ValueError("Not an embedded EPUB resource")
    path = unquote(ref.path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), path)).lstrip("/")


@lru_cache(maxsize=512)
def _embedded_audio(path, file_identity):
    """Return the embedded audio members used by overlays in the EPUB spine."""
    try:
        with ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            opf = next(node.get("full-path") for node in container.iter() if node.tag.rsplit("}", 1)[-1] == "rootfile")
            package = ET.fromstring(archive.read(opf))
            manifest = {node.get("id"): node for node in package.findall("{*}manifest/{*}item")}
            overlays = set()
            for itemref in package.findall("{*}spine/{*}itemref"):
                item = manifest.get(itemref.get("idref"))
                if item is not None and item.get("media-overlay"):
                    overlay = manifest[item.get("media-overlay")]
                    if overlay.get("media-type") != "application/smil+xml":
                        return ()
                    overlays.add(_resolve_member(opf, overlay.get("href")))
            audio_members = set()
            for overlay in overlays:
                smil = ET.fromstring(archive.read(overlay))
                audio_nodes = list(smil.iterfind(".//{*}audio"))
                if not audio_nodes:
                    return ()
                for node in audio_nodes:
                    member = _resolve_member(overlay, node.get("src"))
                    if not archive.getinfo(member).file_size:
                        return ()
                    audio_members.add(member)
            return tuple(sorted(audio_members))
    except (OSError, BadZipFile, ET.ParseError, DefusedXmlException, KeyError,
            RuntimeError, StopIteration, TypeError, ValueError):
        return ()


def embedded_audio(path):
    try:
        path = Path(path)
        stat = path.stat()
        return _embedded_audio(path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    except (OSError, TypeError, ValueError):
        return ()


def audio_matches_single_embedded_track(epub_path, audio_path):
    """Byte equality of one complete track proves a common zero-based clock."""
    members = embedded_audio(epub_path)
    if len(members) != 1:
        return False
    try:
        with ZipFile(epub_path) as archive:
            if archive.getinfo(members[0]).file_size != Path(audio_path).stat().st_size:
                return False
            with archive.open(members[0]) as embedded, open(audio_path, "rb") as external:
                return hashlib.file_digest(embedded, "sha256").digest() == hashlib.file_digest(external, "sha256").digest()
    except (OSError, BadZipFile, KeyError, RuntimeError, TypeError, ValueError):
        return False
