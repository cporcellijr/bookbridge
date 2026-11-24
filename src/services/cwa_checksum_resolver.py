"""Resolve a KOReader document hash to a Calibre book via CWA's checksum history.

Calibre-Web Automated re-exports an EPUB with embedded metadata on every download,
so the copy a reader holds rarely matches the bytes BookBridge hashed. CWA records
the KOReader partial MD5 of each copy it serves in the library's metadata.db
(``book_format_checksums``), before the download is sent. Looking a hash up there
lets a freshly downloaded copy resolve on its first sync, instead of waiting for the
hash reconciler to refetch the book.

The same metadata.db also finds a book's current EPUB by Calibre id, which survives
Calibre renaming the file when a title or author changes.
"""

import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_missing_table_logged = False


def _metadata_db_path(library_dirs: Iterable[Path] = ()) -> Optional[Path]:
    raw = os.environ.get("CALIBRE_LIBRARY_PATH", "").strip()
    if raw:
        path = Path(raw)
        if path.is_dir():
            path = path / "metadata.db"
        return path if path.is_file() else None
    # Only callers that pass library folders accept one that is itself a Calibre
    # library; the checksum lookups keep requiring the setting.
    for library_dir in library_dirs:
        path = Path(library_dir) / "metadata.db"
        try:
            if path.is_file():
                return path
        except OSError as e:
            logger.debug(f"Could not check Calibre metadata.db at '{path}': {e}")
    return None


def calibre_library_root() -> Optional[Path]:
    """The Calibre library folder holding metadata.db, or None when not configured."""
    db_path = _metadata_db_path()
    return db_path.parent.resolve() if db_path else None


def find_calibre_book_id(doc_hash: str) -> Optional[str]:
    """Return the Calibre book id whose EPUB CWA served with this KOReader hash."""
    global _missing_table_logged

    if not doc_hash:
        return None
    db_path = _metadata_db_path()
    if db_path is None:
        return None

    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute(
                "SELECT book FROM book_format_checksums "
                "WHERE checksum = ? AND format = 'EPUB' "
                "ORDER BY created DESC LIMIT 1",
                (doc_hash.lower(),),
            ).fetchone()
    except sqlite3.OperationalError as e:
        # Plain Calibre, or CWA where KOReader sync has never been on, has no checksum table.
        if not _missing_table_logged:
            logger.info(f"CWA checksum lookup unavailable ({db_path}): {e}")
            _missing_table_logged = True
        return None
    except sqlite3.Error as e:
        logger.warning(f"CWA checksum lookup failed: {e}", exc_info=True)
        return None

    if not row or row[0] is None:
        logger.debug(f"CWA checksum lookup: no EPUB checksum row for {doc_hash}")
        return None
    return str(row[0])


def _library_relative_path(path: Path, library_root: Path, library_dirs: Iterable[Path]) -> Optional[Path]:
    """The path of a file relative to the Calibre library, or None when it is outside it.

    The library can be mounted twice (CALIBRE_LIBRARY_PATH and a scanned folder), so a
    file found under a scanned folder counts when the library holds the same file at
    the same relative path.
    """
    try:
        path = path.resolve()
        root = library_root.resolve()
    except OSError:
        return None
    try:
        return path.relative_to(root)
    except ValueError:
        pass
    for library_dir in library_dirs:
        try:
            relative = path.relative_to(Path(library_dir).resolve())
            if os.path.samefile(root / relative, path):
                return relative
        except (OSError, ValueError):
            continue
    return None


def find_calibre_book_id_for_path(path: Path, library_dirs: Iterable[Path] = ()) -> Optional[str]:
    """Return the Calibre book id of an EPUB inside the Calibre library, or None."""
    path = Path(path)
    if path.suffix.lower() != ".epub":
        return None
    db_path = _metadata_db_path(library_dirs)
    if db_path is None:
        return None
    relative = _library_relative_path(path, db_path.parent, library_dirs)
    if relative is None:
        return None
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute(
                "SELECT b.id FROM books b JOIN data d ON d.book = b.id "
                "WHERE b.path = ? AND d.name = ? AND d.format = 'EPUB' LIMIT 1",
                (relative.parent.as_posix(), path.stem),
            ).fetchone()
    except sqlite3.Error as e:
        logger.warning(f"Calibre book lookup by path failed ({db_path}): {e}", exc_info=True)
        return None
    return str(row[0]) if row and row[0] is not None else None


def find_calibre_epub_path(calibre_book_id: str, library_dirs: Iterable[Path] = ()) -> Optional[Path]:
    """Return the library EPUB of a Calibre book, or None when missing or outside the library."""
    db_path = _metadata_db_path(library_dirs)
    if db_path is None or not calibre_book_id:
        return None
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute(
                "SELECT b.path, d.name FROM books b JOIN data d ON d.book = b.id "
                "WHERE b.id = ? AND d.format = 'EPUB' LIMIT 1",
                (calibre_book_id,),
            ).fetchone()
    except sqlite3.Error as e:
        logger.warning(f"CWA library EPUB lookup failed: {e}", exc_info=True)
        return None
    if not row or not row[0] or not row[1]:
        logger.info(f"CWA checksum: Calibre book {calibre_book_id} has no EPUB in metadata.db")
        return None

    root = db_path.parent.resolve()
    path = (root / row[0] / f"{row[1]}.epub").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        logger.warning(f"CWA checksum: refused EPUB path outside the Calibre library for book {calibre_book_id}", exc_info=True)
        return None
    if not path.is_file():
        logger.info(f"CWA checksum: EPUB for Calibre book {calibre_book_id} is missing at {path}")
        return None
    return path
