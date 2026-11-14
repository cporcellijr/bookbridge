"""Processing HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any, phase: str) -> None:
    """Register Processing endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules_by_phase = {
        'queue': (
            ('/job-queue', 'job_queue_page', job_queue_page, ['GET']),
            ('/job-queue/reorder', 'job_queue_reorder', job_queue_reorder, ['POST']),
            ('/job-queue/preflight/ack', 'job_queue_preflight_ack', job_queue_preflight_ack, ['POST']),
        ),
        'shelfmark': (
            ('/shelfmark', 'shelfmark', shelfmark, None),
        ),
    }
    rules = rules_by_phase[phase]
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)


def job_queue_page():
    """Read-only overview of staged work, in-flight batches and recent jobs."""
    web = _web_server()
    user = web.current_user()
    user_id = user.id if user else None
    acquisition_jobs = None

    if web.request.args.get("acquisition_fragment") == "1":
        acquisition_jobs = web.database_service.get_acquisition_jobs(user_id) if user_id is not None else []
        linked_ids = {
            job["linked_abs_id"] for job in acquisition_jobs if job.get("linked_abs_id")
        }
        bridge_statuses = web.database_service.get_linked_book_statuses(user_id, linked_ids)
        # Failure labels depend on the full queue's globally ranked recent-25
        # entries. Preserve that path until its semantics can be narrowed safely.
        if not any(status in {"error", "failed_retry_later", "failed_permanent"}
                   for status in bridge_statuses.values()):
            return web.render_template(
                "_acquisition_jobs_panel.html",
                acquisition_jobs=acquisition_jobs,
                focused_acquisition_job_id=web.request.args.get("acquisition_job", ""),
                bridge_statuses=bridge_statuses,
            )

    books = web.database_service.get_all_books(user_id=user_id)
    books = web._dashboard_visible_books_for_user(books, user)
    with web._VISIBLE_BATCH_LOCK:
        batches = [
            {**batch, 'items': list(batch['items'])}
            for batch in web._VISIBLE_BATCHES.values()
            if batch['user_id'] == user_id
        ]
    with web._READALONG_GENERATION_LOCK:
        active_readalong = set(web._ACTIVE_READALONG_GENERATIONS)
    jobs = [
        *web.database_service.get_latest_jobs(kind=web.JOB_KIND_ALIGNMENT).values(),
        *web.database_service.get_latest_jobs(kind=web.JOB_KIND_READALONG).values(),
    ]
    evidence = web.database_service.get_transcription_preflights(
        user_id, [book.abs_id for book in books],
    ) if user_id is not None else {}
    for book in books:
        row = evidence.get(book.abs_id)
        if row and row["pairing_key"] != web.transcription_preflight_pair_key(book):
            del evidence[book.abs_id]
    web.add_library_comparison(evidence, {book.abs_id for book in books if book.status == "active"})
    alignment_queue = web.database_service.get_pending_alignment_queue()
    alignment_movable_ids = {
        book.abs_id for book in books
        if book.status == 'pending' and user_id is not None and book.user_id == user_id
    }
    snapshot = web.build_job_queue_view(
        web._load_match_queue(), batches, books, jobs, active_readalong,
        alignment_queue=alignment_queue,
        alignment_movable_ids=alignment_movable_ids,
        preflights=evidence,
    )
    if acquisition_jobs is None:
        acquisition_jobs = web.database_service.get_acquisition_jobs(user_id) if user_id is not None else []
    bridge_statuses = {book.abs_id: book.status for book in books if book.abs_id}
    for entry in snapshot["recent"]:
        if entry.get("book_id") and entry.get("status") == "failed":
            bridge_statuses[entry["book_id"]] = "failed"
    for entry in snapshot["pending"]:
        if entry.get("book_id"):
            bridge_statuses[entry["book_id"]] = "pending"
    for entry in snapshot["running"]:
        if entry.get("book_id"):
            bridge_statuses[entry["book_id"]] = "processing"
    panel_context = {
        "acquisition_jobs": acquisition_jobs,
        "focused_acquisition_job_id": web.request.args.get("acquisition_job", ""),
        "bridge_statuses": bridge_statuses,
    }
    if web.request.args.get("acquisition_fragment") == "1":
        return web.render_template("_acquisition_jobs_panel.html", **panel_context)
    return web.render_template(web.ui_template('job_queue.html'), queue_view=snapshot, **panel_context)


def job_queue_reorder():
    """Move only an owned, still-pending entry in the chosen queue."""
    web = _web_server()
    key = web.request.form.get('queue_key', '')
    direction = web.request.form.get('direction', '')
    queue_kind = web.request.form.get('queue', 'staged')
    if not key or direction not in {'up', 'down', 'top'} or queue_kind not in {'staged', 'alignment'}:
        web.flash("That move isn't valid.", "warning")
    elif queue_kind == 'alignment':
        user = web.current_user()
        result = web.database_service.move_pending_alignment_book(
            user.id if user is not None else None, key, direction,
        )
        if result == 'missing':
            web.flash("That book is no longer waiting for alignment.", "warning")
    else:
        result = web._match_queue_move(key, direction)
        if result == 'missing':
            web.flash("That book is no longer waiting in Add Book.", "warning")
        elif result == 'ambiguous':
            web.flash("That book couldn't be identified; refresh and try again.", "warning")
    return web.redirect(web.url_for('job_queue_page'))


def job_queue_preflight_ack():
    """Acknowledge current per-reader evidence; never override match refusal."""
    web = _web_server()
    user = web.current_user()
    if user is None:
        web.abort(401)
    abs_id = web.request.form.get("abs_id", "")
    identity = web.request.form.get("identity", "")
    if not web.database_service.is_user_linked(user.id, abs_id):
        web.abort(404)
    if not web.database_service.acknowledge_transcription_preflight(user.id, abs_id, identity):
        web.abort(409)
    web.flash("Pre-flight advisory acknowledged. The final content-match guard remains active.", "success")
    return web.redirect(web.url_for("job_queue_page"))


def shelfmark() -> object:
    """Redirect to SHELFMARK_URL (library when unset)."""
    web = _web_server()
    url = web.os.environ.get("SHELFMARK_URL")
    if not url:
        return web.redirect(web.url_for('index'))

    # Case-insensitive sanitization for the external destination.
    if not url.lower().startswith(('http://', 'https://')):
        url = f"http://{url}"
    return web.redirect(url)
