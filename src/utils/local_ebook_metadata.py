"""Title/author metadata for library ebook files, read from Calibre's metadata.opf or the
EPUB's own OPF. Shared by suggestion scoring and any other Local File consumer."""
import logging
import threading
import unicodedata
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Tuple
from xml.etree import ElementTree

from src.utils.series_metadata import _local_tag, _locate_opf_path

logger = logging.getLogger(__name__)

_OPF_NAMESPACE = "{http://www.idpf.org/2007/opf}"
_EBOOK_SUFFIXES = frozenset({".epub", ".kepub", ".pdf", ".mobi", ".azw", ".azw3", ".cbz", ".cbr", ".fb2"})
_CACHE_MAX = 4096
_cache: "OrderedDict[tuple, Tuple[str, str]]" = OrderedDict()
_cache_lock = threading.Lock()


def opf_title_authors(opf_xml: bytes) -> Tuple[str, str]:
    """Return ``(title, "Author & Author")`` from OPF package XML.

    Creators with a role other than author (translator, editor) are skipped; a
    creator without a role counts as an author.
    """
    title, authors = "", []
    root = ElementTree.fromstring(opf_xml)
    refined_roles = {}
    for element in root.iter():
        refines = element.attrib.get("refines", "")
        role = (element.text or "").strip().lower()
        if _local_tag(element) == "meta" and element.attrib.get("property") == "role" and refines.startswith("#") and role:
            refined_roles.setdefault(refines[1:], set()).add(role)
    for element in root.iter():
        tag = _local_tag(element)
        text = (element.text or "").strip()
        if not text:
            continue
        if tag == "title" and not title:
            title = text
        elif tag == "creator":
            roles = set(refined_roles.get(element.attrib.get("id"), ()))
            role = (element.attrib.get(_OPF_NAMESPACE + "role") or element.attrib.get("role") or "").strip().lower()
            if role:
                roles.add(role)
            if not roles or "aut" in roles:
                authors.append(text)
    return unicodedata.normalize("NFC", title), unicodedata.normalize("NFC", " & ".join(authors))


def _sole_book_in_folder(path: Path) -> bool:
    """True when every ebook beside *path* shares its stem (one Calibre book, many formats)."""
    stems = {p.stem for p in path.parent.iterdir() if p.suffix.lower() in _EBOOK_SUFFIXES}
    return stems <= {path.stem}


def _read_uncached(path: Path) -> Tuple[str, str]:
    opf_file = path.parent / "metadata.opf"
    try:
        if opf_file.is_file() and _sole_book_in_folder(path):
            title, authors = opf_title_authors(opf_file.read_bytes())
            if title:
                return title, authors
    except Exception:
        logger.debug("Local ebook metadata: could not read %s", opf_file, exc_info=True)
    if path.suffix.lower() == ".epub":
        try:
            with zipfile.ZipFile(path) as zf:
                opf_path = _locate_opf_path(zf)
                if opf_path:
                    return opf_title_authors(zf.read(opf_path))
        except Exception:
            logger.debug("Local ebook metadata: could not read %s", path, exc_info=True)
    return "", ""


def read_local_ebook_metadata(path: Any) -> Tuple[str, str]:
    """Return ``(title, authors)`` for a library file; empty strings when unreadable.

    Never raises. Results are cached per (resolved path, mtime, size) so repeated scans
    only re-read files that changed.
    """
    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        opf_file = resolved.parent / "metadata.opf"
        opf_stat = opf_file.stat() if opf_file.exists() else None
        key = (
            str(resolved), stat.st_mtime_ns, stat.st_size, resolved.parent.stat().st_mtime_ns,
            (opf_stat.st_mtime_ns, opf_stat.st_size) if opf_stat else None,
        )
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
        value = _read_uncached(resolved)
        with _cache_lock:
            _cache[key] = value
            while len(_cache) > _CACHE_MAX:
                _cache.popitem(last=False)
        return value
    except Exception:
        logger.debug("Local ebook metadata: could not inspect %s", path, exc_info=True)
        return "", ""


def clear_metadata_cache() -> None:
    """Drop every cached metadata result (tests, or after bulk library changes)."""
    with _cache_lock:
        _cache.clear()
