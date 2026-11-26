"""Readalong HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Readalong endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/readalong-epub/<abs_id>', 'generate_readalong_epub', generate_readalong_epub, ['POST']),
        ('/api/readalong-epub/<abs_id>/status', 'readalong_epub_status', readalong_epub_status, ['GET']),
        ('/api/readalong-epub/<abs_id>/remove', 'remove_readalong_epub_route', remove_readalong_epub_route, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def generate_readalong_epub(abs_id: str):
    """Queue background generation + delivery of a read-along EPUB for `abs_id`.

    Async: transcode + repackage takes minutes, so this
    returns immediately once eligibility is confirmed and the real work runs
    on a user-scoped background thread. Eligibility mirrors what
    `deliver_readalong_epub`/`build_readalong_epub` actually require that is
    cheap to check up front (sync mode, audio source, alignment method);
    resolvability of the live BookOrbit audio entry itself can only be
    confirmed by the worker's own call to `resolve_audiobook_folder`.
    """
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)

    if web._has_linked_readalong(book, resolve_local=True) or abs_id in (web.database_service.get_readalong_ready_book_ids() or ()):
        return web.jsonify({"success": True, "status": "existing"})

    if getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return web.jsonify({
            "success": False,
            "error": "This mapping has no audiobook to generate a read-along from.",
        }), 400

    if getattr(book, "audio_source", None) != "BookOrbit":
        return web.jsonify({
            "success": False,
            "error": "Read-along generation requires a BookOrbit audio source.",
        }), 400

    align_method = web.database_service.get_alignment_method(abs_id)
    if (align_method or "") not in ("ctc", "lexical", "lexical_timed"):
        return web.jsonify({
            "success": False,
            "error": "This book has no CTC or lexical alignment map yet.",
        }), 400

    # Admission is process-wide so manual requests and post-forge hooks cannot
    # start two workers for the same book. Persisted incomplete rows are only a
    # status record: after a restart there is no live worker behind one, so it
    # must not block a retry.
    if not web._claim_readalong_generation(abs_id):
        return web.jsonify({
            "success": False,
            "error": "Read-along generation is already in progress for this book.",
        }), 409

    from src.db.models import Job
    job = None
    try:
        job = web.database_service.save_job(
            Job(
                abs_id=abs_id, last_attempt=web.time.time(), retry_count=0, progress=0.0, last_error=None,
                kind=web.JOB_KIND_READALONG, stage="queued",
            )
        )
        web._spawn_user_background(
            web._readalong_epub_worker, abs_id, job.id, label=f"readalong-epub-{abs_id}",
        )
    except Exception as e:
        if job is not None and getattr(job, "id", None) is not None:
            try:
                web.database_service.update_job_by_id(
                    job.id, last_error=f"Read-along generation failed to start: {e}",
                )
            except Exception as update_error:
                web.logger.debug("Read-along: could not record start failure for '%s': %s", abs_id, update_error)
        web._release_readalong_generation(abs_id)
        web.logger.error(
            "❌ Could not queue read-along generation for '%s': %s",
            web.sanitize_log_data(abs_id), e, exc_info=True,
        )
        return web.jsonify({"success": False, "error": "Could not queue read-along generation."}), 500

    web.logger.info(
        "📖 Read-along EPUB generation queued for %s", web.sanitize_log_data(book.abs_title or abs_id)
    )
    return web.jsonify({"success": True, "status": "queued"})


def readalong_epub_status(abs_id: str):
    """Poll the outcome of the most recent read-along generation job for `abs_id`.

    Reads the `Job` row `generate_readalong_epub` created/updates, scoped to
    `kind=JOB_KIND_READALONG` -- the `jobs` table also carries Forge & Match
    rows (`_record_forge_match_job`) and alignment-repair rows for the same
    `abs_id`, and without the kind filter a job from one of those flows
    running on the same book at the same time could be newer and would be
    read here as if it were the read-along job's own status.

    Alongside the pre-existing `state` (idle/running/done/failed) contract,
    also surfaces `stage` (the raw `Job.stage` key, e.g. 'transcoding_audio'),
    `stage_label` (its human-readable form), and `percent` (0-100, from
    `Job.progress`) so the dashboard poller can show real signal for a
    multi-minute build instead of a static "generating" message. These are
    additive -- every pre-existing key/shape is unchanged.
    """
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)

    job = web.database_service.get_latest_job(abs_id, kind=web.JOB_KIND_READALONG)
    if job is None:
        return web.jsonify({"success": True, "state": "idle"})

    # Defensive against non-string/non-numeric attributes (e.g. a bare
    # unittest.mock.Mock() in a test that only set progress/last_error) --
    # never let a malformed/legacy row break JSON serialization.
    raw_stage = getattr(job, "stage", None)
    stage = raw_stage if isinstance(raw_stage, str) and raw_stage else None
    stage_label = web._readalong_stage_label(stage)
    raw_progress = getattr(job, "progress", None)
    progress_fraction = raw_progress if isinstance(raw_progress, (int, float)) else 0.0
    percent = int(round(max(0.0, min(1.0, progress_fraction)) * 100))

    if job.progress and job.progress >= 1.0:
        payload = {
            "success": True, "state": "done",
            "stage": stage, "stage_label": stage_label, "percent": percent,
        }
        if job.last_error:
            payload["warning"] = job.last_error
        return web.jsonify(payload)
    if job.last_error:
        return web.jsonify({
            "success": True, "state": "failed", "error": job.last_error,
            "stage": stage, "stage_label": stage_label, "percent": percent,
        })
    return web.jsonify({
        "success": True, "state": "running",
        "stage": stage, "stage_label": stage_label, "percent": percent,
    })


def remove_readalong_epub_route(abs_id: str):
    """Remove a previously-delivered read-along EPUB from BookOrbit and disk.

    Synchronous, unlike generation: this is a single BookOrbit delete call plus
    a local `unlink`, not a transcode/repackage, so it does not need a
    background job. Resolution mirrors `deliver_readalong_epub`'s own
    (`resolve_bookorbit_book_id` -> `resolve_audiobook_folder` ->
    `_readalong_filename`) so the computed path is exactly the one delivery
    would have written to; every resolution failure is reported as "nothing to
    remove" rather than an error, since there is nothing this action could
    have undone.
    """
    web = _web_server()
    book = web.database_service.get_book(abs_id)
    if not book:
        return web.jsonify({"success": False, "error": "Book not found"}), 404

    user = web.current_user()
    if not web._user_may_modify_book(user, abs_id):
        return web._forbidden_book_response(json_response=True)

    if getattr(book, "audio_source", None) != "BookOrbit":
        return web.jsonify({"success": True, "removed": False, "message": "No BookOrbit audio entry for this book."})

    clients = web.uc()
    audio_sync_client = clients.sync_clients.get("BookOrbitAudio")
    audio_book_id = audio_sync_client.resolve_bookorbit_book_id(book) if audio_sync_client else None
    if audio_book_id is None:
        return web.jsonify({"success": True, "removed": False, "message": "No resolvable BookOrbit audio entry for this book."})

    epub_filename = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
    if not epub_filename:
        return web.jsonify({"success": True, "removed": False, "message": "No source EPUB on file."})
    try:
        epub_path = web.container.ebook_parser().resolve_book_path(epub_filename)
    except FileNotFoundError:
        return web.jsonify({"success": True, "removed": False, "message": "Source EPUB not found on disk."})

    resolved = web.resolve_audiobook_folder(clients.bookorbit_client, audio_book_id)
    output_name = web._readalong_filename(web.Path(epub_path))
    output_path = resolved.folder / output_name if resolved else web.Path(output_name)
    ok = web._remove_readalong_epub_file(clients.bookorbit_client, audio_book_id, output_path)
    if ok:
        web.logger.info("🗑️ Removed read-along EPUB for %s", web.sanitize_log_data(book.abs_title or abs_id))
        # The dashboard badge and the status poll read the latest read-along job;
        # a removed read-along must stop showing as ready.
        web.database_service.delete_jobs_for_book(abs_id, kind=web.JOB_KIND_READALONG)
        return web.jsonify({"success": True, "removed": True})
    return web.jsonify({"success": False, "error": "Could not fully remove the read-along EPUB; see server logs."}), 500
