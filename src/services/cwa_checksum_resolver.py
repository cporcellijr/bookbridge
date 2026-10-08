"""Resolve a KOReader document hash to a Calibre book via CWA's checksum history.

Calibre-Web Automated re-exports an EPUB with embedded metadata on every download,
so the copy a reader holds rarely matches the bytes BookBridge hashed. CWA records
the KOReader partial MD5 of each copy it serves in the library's metadata.db
(``book_format_checksums``), before the download is sent. Looking a hash up there
lets a freshly downloaded copy resolve on its first sync, instead of waiting for the
hash reconciler to refetch the book.
"""

import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_missing_table_logged = False


def _metadata_db_path() -> Optional[Path]:
    raw = os.environ.get("CALIBRE_LIBRARY_PATH", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_dir():
        path = path / "metadata.db"
    return path if path.is_file() else None


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
        logger.warning(f"CWA checksum lookup failed: {e}")
        return None

    return str(row[0]) if row and row[0] is not None else None
