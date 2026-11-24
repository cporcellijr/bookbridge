"""Storyteller maintenance helpers; extracted without behavior changes."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")

def _run_storyteller_backfill():
    """
    Bulk backfill storyteller transcripts for currently matched storyteller books.
    """
    web = _web_server()
    assets_dir_raw = web.os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
    if not assets_dir_raw:
        return {
            "success": False,
            "error": "STORYTELLER_ASSETS_DIR is not configured",
            "scanned": 0,
            "ingested": 0,
            "missing": 0,
            "failed": 0,
            "aligned": 0,
            "duration_seconds": 0.0,
        }, 400

    started_at = web.time.time()
    books = web.database_service.get_all_books() if web.database_service else []
    storyteller_books = [
        b for b in books
        if getattr(b, "storyteller_uuid", None) or getattr(b, "transcript_source", None) == "storyteller"
    ]

    summary = {
        "success": True,
        "scanned": 0,
        "ingested": 0,
        "missing": 0,
        "failed": 0,
        "aligned": 0,
        "duration_seconds": 0.0,
    }

    abs_client = web.container.abs_client() if web.container else None
    alignment_service = getattr(web.manager, "alignment_service", None) if web.manager else None
    ebook_parser = web.container.ebook_parser() if web.container else None

    for book in storyteller_books:
        summary["scanned"] += 1
        abs_id = book.abs_id
        try:
            item_details = abs_client.get_item_details(abs_id) if abs_client else None
            chapters = item_details.get("media", {}).get("chapters", []) if item_details else []
            manifest_path = web.ingest_storyteller_transcripts(abs_id, book.abs_title or "", chapters)
            if not manifest_path:
                summary["missing"] += 1
                continue

            book.transcript_source = "storyteller"
            book.transcript_file = manifest_path
            summary["ingested"] += 1

            aligned = False
            if alignment_service:
                storyteller_transcript = web.StorytellerTranscript(manifest_path)
                book_text = ""
                if ebook_parser and book.ebook_filename:
                    try:
                        epub_filename = book.original_ebook_filename or book.ebook_filename
                        epub_path = web.safe_cache_path(web.container.epub_cache_dir(), epub_filename)
                        if epub_path and epub_path.exists():
                            book_text, _ = ebook_parser.extract_text_and_map(epub_path)
                    except Exception as e:
                        web.logger.warning(f"Could not extract text for storyteller backfill: {e}", exc_info=True)
                        
                aligned = alignment_service.align_storyteller_and_store(abs_id, storyteller_transcript, ebook_text=book_text)
                if aligned:
                    summary["aligned"] += 1

            if aligned:
                book.transcript_file = "DB_MANAGED"
                if getattr(book, "status", None) in (None, "", "pending", "processing", "failed_retry_later", "failed_permanent", "crashed"):
                    book.status = "active"
            else:
                book.status = "pending"

            web.database_service.save_book(book)
        except Exception as e:
            summary["failed"] += 1
            web.logger.warning(f"Storyteller backfill failed for '{abs_id}': {e}", exc_info=True)

    summary["duration_seconds"] = round(web.time.time() - started_at, 3)
    web.logger.info(
        "Storyteller backfill summary: "
        f"scanned={summary['scanned']} ingested={summary['ingested']} "
        f"aligned={summary['aligned']} missing={summary['missing']} failed={summary['failed']} "
        f"duration={summary['duration_seconds']}s"
    )
    return summary, 200


def _extract_series_from_title(title: str) -> tuple:
    """Return (series_name, series_sequence) parsed out of a numbered title."""
    web = _web_server()
    return web._series_from_title(title)


def _series_seq_equal(stored: "float | int | str | None", resolved: "float | None") -> bool:
    """True when a stored series sequence already matches a freshly resolved one."""
    if stored is None and resolved is None:
        return True
    if stored is None or resolved is None:
        return False
    try:
        return abs(float(stored) - float(resolved)) < 1e-9
    except (TypeError, ValueError):
        return False


def _series_refresh_action(stored_name: "str | None", stored_seq: "float | int | str | None",
                           resolution) -> str:
    """Decide what a resolved series means for a stored row.

    Returns ``update``, ``unchanged``, ``clear``, ``keep`` or ``none``.
    ``clear`` is only ever returned when a service that owns the book actually
    answered — silence from an offline or unconfigured library must leave the
    stored series alone rather than read as "the series was deleted".
    """
    had_series = bool((stored_name or "").strip())
    if resolution.name:
        if had_series and resolution.name == stored_name:
            if _series_seq_equal(stored_seq, resolution.sequence):
                return "unchanged"
            # Never trade a known volume number for an unknown one. Some
            # libraries carry the series name but no index, and dropping the
            # stored number would sort that book to the end of its own series.
            if resolution.sequence is None and stored_seq is not None:
                return "unchanged"
        return "update"
    if had_series:
        return "clear" if resolution.service_answered else "keep"
    return "none"


def _series_backfill_clients() -> dict:
    """Return the clients the series backfill resolves against, keyed by kwarg name."""
    web = _web_server()
    return {
        "abs_client": web.container.abs_client(),
        "bookorbit_client": web.container.bookorbit_client(),
        "booklore_client": web.container.booklore_client(),
        "kavita_client": web.container.kavita_client(),
        "ebook_parser": web.container.ebook_parser(),
    }

