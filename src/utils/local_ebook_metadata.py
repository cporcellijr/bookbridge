"""Title, author, and series metadata for library ebook files, read from the Calibre
library's metadata.db, its metadata.opf, or the EPUB's own OPF. Shared by suggestion
scoring and any other Local File consumer."""
import logging
import sqlite3
import threading
import unicodedata
import zipfile
from collections import OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree

from src.utils.series_metadata import (
    SeriesTuple,
    _extract_calibre_series_from_opf,
    _extract_epub3_collection_from_opf,
    _local_tag,
    _locate_opf_path,
)

logger = logging.getLogger(__name__)

_OPF_NAMESPACE = "{http://www.idpf.org/2007/opf}"
_EBOOK_SUFFIXES = frozenset({".epub", ".kepub", ".pdf", ".mobi", ".azw", ".azw3", ".cbz", ".cbr", ".fb2"})
_CACHE_MAX = 4096
_Record = Tuple[str, str, Optional[str], Optional[float]]
_EMPTY: _Record = ("", "", None, None)
_cache: "OrderedDict[tuple, _Record]" = OrderedDict()
_cache_lock = threading.Lock()
_LIBRARY_DB = "metadata.db"
_library_cache: Dict[str, Tuple[tuple, Dict[str, _Record]]] = {}


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


def opf_series(opf_xml: bytes) -> SeriesTuple:
    """Return ``(series_name, series_sequence)`` from OPF package XML."""
    root = ElementTree.fromstring(opf_xml)
    name, sequence = _extract_calibre_series_from_opf(root)
    if name:
        return name, sequence
    return _extract_epub3_collection_from_opf(root)


def _opf_record(opf_xml: bytes) -> _Record:
    return (*opf_title_authors(opf_xml), *opf_series(opf_xml))


def _library_state(db_file: Path) -> Optional[tuple]:
    """Size and mtime of metadata.db and its WAL file; a committed edit changes one of them."""
    try:
        db_stat = db_file.stat()
    except OSError:
        return None
    wal_file = db_file.with_name(db_file.name + "-wal")
    wal_stat = wal_file.stat() if wal_file.exists() else None
    return (
        db_stat.st_mtime_ns, db_stat.st_size,
        (wal_stat.st_mtime_ns, wal_stat.st_size) if wal_stat else None,
    )


def _load_library(db_file: Path) -> Dict[str, _Record]:
    """Map each book folder ("Author/Title (id)") in a Calibre library to its record."""
    # Read-only, since the library is often mounted read-only and Calibre-Web owns writes.
    with closing(sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True, timeout=1)) as conn:
        books = conn.execute("SELECT id, path, title, series_index FROM books").fetchall()
        authors: Dict[int, List[str]] = {}
        for book_id, name in conn.execute(
            "SELECT l.book, a.name FROM books_authors_link l JOIN authors a ON a.id = l.author ORDER BY l.id"
        ):
            # Calibre stores a comma inside an author's name as "|".
            authors.setdefault(book_id, []).append((name or "").replace("|", ","))
        series = dict(conn.execute(
            "SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id = l.series"
        ).fetchall())
    records = {}
    for book_id, folder, title, index in books:
        name = series.get(book_id)
        records[folder] = (
            unicodedata.normalize("NFC", title or ""),
            unicodedata.normalize("NFC", " & ".join(authors.get(book_id, []))),
            name,
            float(index) if name and index is not None else None,
        )
    return records


def _library_record(path: Path) -> Optional[_Record]:
    """The book's record in metadata.db, which Calibre keeps two folders above the book's."""
    if len(path.parents) < 3:
        return None
    root = path.parents[2]
    db_file = root / _LIBRARY_DB
    state = _library_state(db_file)
    if state is None:
        return None
    with _cache_lock:
        cached = _library_cache.get(str(db_file))
    if cached and cached[0] == state:
        records = cached[1]
    else:
        try:
            records = _load_library(db_file)
        except Exception:
            logger.debug("Local ebook metadata: could not read %s", db_file, exc_info=True)
            # Cached as empty so an unreadable database isn't reopened for every book.
            records = {}
        with _cache_lock:
            _library_cache[str(db_file)] = (state, records)
    record = records.get(path.parent.relative_to(root).as_posix())
    return record if record and record[0] else None


def _sole_book_in_folder(path: Path) -> bool:
    """True when every ebook beside *path* shares its stem (one Calibre book, many formats)."""
    stems = {p.stem for p in path.parent.iterdir() if p.suffix.lower() in _EBOOK_SUFFIXES}
    return stems <= {path.stem}


def _read_uncached(path: Path) -> _Record:
    opf_file = path.parent / "metadata.opf"
    try:
        if opf_file.is_file() and _sole_book_in_folder(path):
            record = _opf_record(opf_file.read_bytes())
            if record[0]:
                return record
    except Exception:
        logger.debug("Local ebook metadata: could not read %s", opf_file, exc_info=True)
    if path.suffix.lower() == ".epub":
        try:
            with zipfile.ZipFile(path) as zf:
                opf_path = _locate_opf_path(zf)
                if opf_path:
                    return _opf_record(zf.read(opf_path))
        except Exception:
            logger.debug("Local ebook metadata: could not read %s", path, exc_info=True)
    return _EMPTY


def read_local_ebook_metadata(path: Any) -> Tuple[str, str]:
    """Return ``(title, authors)`` for a library file; empty strings when unreadable.

    Calibre-Web edits metadata.db right away but rewrites metadata.opf only in its
    scheduled backup, so the library database is read first. Never raises. Results are
    cached per (resolved path, mtime, size) so repeated scans only re-read files that
    changed.
    """
    return _cached_record(path)[:2]


def read_local_ebook_series(path: Any) -> SeriesTuple:
    """Return ``(series_name, series_sequence)`` for a library file; Nones when unreadable.

    Read from the same source, and cached the same way, as :func:`read_local_ebook_metadata`.
    """
    return _cached_record(path)[2:]


def _cached_record(path: Any) -> _Record:
    try:
        resolved = Path(path).resolve()
        record = _library_record(resolved)
        if record:
            return record
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
        return _EMPTY


def clear_metadata_cache() -> None:
    """Drop every cached metadata result (tests, or after bulk library changes)."""
    with _cache_lock:
        _cache.clear()
        _library_cache.clear()
