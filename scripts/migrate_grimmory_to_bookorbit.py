#!/usr/bin/env python3
"""Re-point existing Grimmory (BookLore) ebook mappings to BookOrbit.

Why: switching ebook libraries does NOT require re-matching. A match's valuable
data — the ABS<->ebook link and the `kosync_doc_id` content hash — is
source-independent. Only the `ebook_source` / `ebook_source_id` pointer is
library-specific. Because Grimmory and BookOrbit point at the same ebook files
(same /books disk, identical filenames + content hashes), this backfill resolves
each Grimmory-sourced book in BookOrbit by filename and flips the pointer in
place, leaving the audio link, kosync hash, and progress untouched.

Run INSIDE the abs-kosync-enhanced container so it sees the live DB + settings:

    # dry run (default): report what would change, touch nothing
    docker exec abs_kosync_enhanced python -m scripts.migrate_grimmory_to_bookorbit

    # apply
    docker exec abs_kosync_enhanced python -m scripts.migrate_grimmory_to_bookorbit --apply

Prerequisites: BookOrbit must be enabled/configured in BookBridge settings and
its ebook library scanned, so filenames resolve. Re-runnable / idempotent.
"""

import argparse
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db.migration_utils import initialize_database
from src.utils.config_loader import ConfigLoader
from src.utils.di_container import create_container

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("bookorbit-backfill")


def _is_grimmory_sourced(book) -> bool:
    # Grimmory mappings are tagged under any of these variants depending on the
    # match path that created them.
    src = (getattr(book, "ebook_source", None) or "").strip().lower()
    return src in ("booklore", "grimmory")


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-point Grimmory ebook mappings to BookOrbit.")
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry run).")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N books (0 = all).")
    args = parser.parse_args()

    database_service = initialize_database(os.environ.get("DATA_DIR", "/data"))
    ConfigLoader.bootstrap_config(database_service)
    ConfigLoader.load_settings(database_service)
    container = create_container()
    bookorbit = container.bookorbit_client()

    if not bookorbit.is_configured():
        logger.error("BookOrbit is not configured/enabled in settings — aborting.")
        return 2

    # Warm the BookOrbit cache once up front.
    bookorbit.clear_and_refresh()

    books = database_service.get_all_books()
    candidates = [b for b in books if _is_grimmory_sourced(b)]
    if args.limit:
        candidates = candidates[: args.limit]

    logger.info(
        "%sScanning %d Grimmory-sourced book(s) of %d total.",
        "" if args.apply else "[DRY RUN] ",
        len(candidates), len(books),
    )

    migrated = skipped_already = unresolved = 0
    for book in candidates:
        filename = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
        if not filename:
            unresolved += 1
            logger.warning("  ? %s — no ebook filename; skipped", book.abs_id)
            continue

        info = bookorbit.find_book_by_filename(filename)
        if not info or not info.get("id"):
            unresolved += 1
            logger.warning("  ✗ %s — '%s' not found in BookOrbit (scan complete?)", book.abs_id, filename)
            continue

        new_id = str(info["id"])
        if (getattr(book, "ebook_source", None) == "BookOrbit"
                and str(getattr(book, "ebook_source_id", "")) == new_id):
            skipped_already += 1
            continue

        logger.info(
            "  → %s  '%s'  BookLore(%s) → BookOrbit(%s)",
            book.abs_id, filename, getattr(book, "ebook_source_id", None), new_id,
        )
        if args.apply:
            book.ebook_source = "BookOrbit"
            book.ebook_source_id = new_id
            database_service.save_book(book)
            migrated += 1

    logger.info(
        "%sDone. matched=%d already=%d unresolved=%d",
        "" if args.apply else "[DRY RUN] would migrate=%d; " % (len(candidates) - skipped_already - unresolved),
        migrated if args.apply else 0, skipped_already, unresolved,
    )
    if not args.apply:
        logger.info("Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
