"""C33 helpers extracted from web_server with no behavior change."""

from importlib import import_module
from typing import Optional

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


_READALONG_STAGE_LABELS = {
    "queued": "Queued",
    "resolving_audio": "Resolving audio",
    "converting_epub": "Converting EPUB 2 to EPUB 3",
    "parsing_epub": "Parsing EPUB",
    "transcoding_audio": "Transcoding audio",
    "building_overlays": "Building overlays",
    "packaging": "Packaging",
    "delivering": "Delivering to BookOrbit",
}


def _readalong_stage_label(stage: Optional[str]) -> str:
    """Human-readable label for a read-along job's raw ``Job.stage`` value.

    Anything that is not a non-empty string (``None`` -- no stage recorded
    yet, or a pre-migration row) falls back to "Working" rather than raising
    or showing a blank/placeholder value in the UI.
    """
    if not isinstance(stage, str) or not stage:
        return "Working"
    return _READALONG_STAGE_LABELS.get(stage, stage)


def _readalong_epub_worker(abs_id: str, job_id: Optional[int] = None) -> None:
    """Background worker: generate (Phases 1-4) and deliver (Phase 5) a
    read-along EPUB for one book.

    Runs off the request thread via `_spawn_user_background`, which has
    already rebound the triggering user's contextvars before calling this --
    BookOrbit is configured per user, and a bare `threading.Thread` would
    silently resolve the admin/global client instead (CLAUDE.md failure mode
    #5). Progress/outcome are persisted through the existing `Job` model (the
    same mechanism `_record_forge_match_job` uses) so
    `/api/readalong-epub/<abs_id>/status` can report the real result even
    after the triggering request has long since returned.

    Never touches `book.ebook_filename` / `book.original_ebook_filename` --
    `deliver_readalong_epub` itself is the one place that would, and it does
    not (see its own docstring).

    **Stage progress**: `deliver_readalong_epub`/`build_readalong_epub` call
    back into `_report_progress` below at each real stage transition
    (resolving the audio entry, converting EPUB 2 -> 3, parsing, the
    dominant ffmpeg transcode, building overlays, packaging, delivering) so
    `/status` can show real signal instead of a static "generating" message
    for however many minutes a long book takes. A failed progress *write* is
    logged and swallowed here (and
    again, defensively, inside the callback chain itself via
    `readalong_builder._safe_progress`) -- it must never be the reason a
    book fails to generate.

    ``job_id`` binds every status write to this worker's own row. It is
    optional because Forge records the row on a best-effort basis; when row
    creation fails, status writes are skipped rather than guessed by recency.
    """
    web = _web_server()
    def _update_job(**kwargs) -> None:
        if job_id is None:
            return
        try:
            web.database_service.update_job_by_id(job_id, **kwargs)
        except Exception as e:
            web.logger.warning(
                "Could not record read-along job state for '%s' (job_id=%s, %s): %s",
                web.sanitize_log_data(abs_id), job_id, kwargs, e, exc_info=True,
            )

    def _report_progress(stage: str, fraction: float) -> None:
        _update_job(progress=fraction, stage=stage)

    try:
        book = web.database_service.get_book(abs_id)
        if not book:
            _update_job(last_error="Book no longer exists")
            return

        alignment_service = getattr(web.manager, "alignment_service", None) if web.manager else None
        if alignment_service is None:
            _update_job(last_error="Alignment service unavailable")
            return

        clients = web.uc()
        ebook_sync_client = clients.sync_clients.get("BookOrbit")
        audio_sync_client = clients.sync_clients.get("BookOrbitAudio")
        if audio_sync_client is None:
            _update_job(last_error="BookOrbit audio sync is not available")
            return

        result = web.deliver_readalong_epub(
            parser=web.container.ebook_parser(),
            alignment_service=alignment_service,
            bookorbit_client=clients.bookorbit_client,
            ebook_sync_client=ebook_sync_client,
            audio_sync_client=audio_sync_client,
            book=book,
            progress_callback=_report_progress,
        )
        if result is None:
            _update_job(
                last_error="Read-along generation was refused -- see server logs for the exact reason.",
            )
            return

        if not result.confirmed:
            _update_job(
                progress=1.0,
                last_error=(
                    "Generated and delivered, but BookOrbit has not confirmed it "
                    f"enabled (last status: {result.read_aloud_sync})."
                ),
            )
            web.logger.warning(
                "⚠️ Read-along EPUB delivered for '%s' but not confirmed enabled",
                web.sanitize_log_data(abs_id),
            )
            return

        _update_job(progress=1.0, last_error=None)
        web.logger.info(
            "📖 Read-along EPUB ready for %s", web.sanitize_log_data(book.abs_title or abs_id)
        )
    except Exception as e:
        web.logger.error(
            "❌ Read-along generation failed for '%s': %s", web.sanitize_log_data(abs_id), e, exc_info=True,
        )
        _update_job(last_error=f"Unexpected error: {e}")
    finally:
        # Manual and post-forge requests share this process-wide reservation.
        # The post-forge wrapper releases only when it cannot hand work here.
        web._release_readalong_generation(abs_id)
