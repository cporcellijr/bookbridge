"""Read-only snapshot helpers for the Job Queue page (issue #529).

This deliberately does not schedule work: the Add Book staging list, in-process
batch handoffs and database jobs have different lifetimes and no global FIFO.
"""
from datetime import datetime, timezone
from math import isfinite


def _title(item: dict) -> str:
    return (item.get("audio_title") or item.get("abs_title") or
            item.get("title") or item.get("abs_id") or "Untitled book")


def _percent(value: object) -> float:
    try:
        fraction = float(value)
        return round(max(0, min(100, fraction * 100)), 1) if isfinite(fraction) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _entry(title: str, operation: str, status: str, *, detail: str = "",
           epub3: bool = False, percent: float | None = None,
           timestamp: float | None = None, order: int | None = None,
           queue_key: str | None = None, can_move_up: bool = False,
           can_move_down: bool = False) -> dict:
    return {
        "title": str(title),
        "operation": operation,
        "status": status,
        "detail": str(detail or ""),
        "epub3": bool(epub3),
        "percent": percent,
        "timestamp": timestamp,
        "order": order,
        "queue_key": queue_key,
        "can_move_up": can_move_up,
        "can_move_down": can_move_down,
    }


def build_job_queue_view(staged_items: list[dict], batches: list[dict],
                         books: list, jobs: list, active_readalong_ids: set[str],
                         now: float | None = None) -> dict[str, list[dict]]:
    """Combine visible work sources without pretending they share a scheduler.

    `batches` contains in-process handoffs only. A restart loses those entries,
    so persisted book/job states are shown separately and incomplete read-along
    rows without a live reservation are reported as interrupted, not running.
    """
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    pending, running, recent = [], [], []

    staged_keys = [item.get('bridge_key') or item.get('abs_id') for item in staged_items]
    for idx, item in enumerate(staged_items, 1):
        key = staged_keys[idx - 1]
        # A missing or duplicate identifier cannot be safely moved by a request.
        can_reorder = bool(isinstance(key, str) and key and staged_keys.count(key) == 1)
        pending.append(_entry(
            _title(item), "Add Book", "staged",
            detail="In Add Book; not yet submitted for processing",
            epub3=item.get("readalong_epub_requested"), order=idx,
            queue_key=key if can_reorder else None,
            can_move_up=can_reorder and idx > 1,
            can_move_down=can_reorder and idx < len(staged_items),
        ))

    for batch in batches:
        items = batch["items"]
        current = batch["current"]
        for idx, item in enumerate(items):
            # Earlier items were already handed off and no longer wait here.
            if idx < current:
                continue
            entry = _entry(
                _title(item), batch["label"], "running" if idx == current else "queued",
                detail=("Submitting this book to its worker" if idx == current
                        else "Waiting in submitted batch"),
                epub3=item.get("readalong_epub_requested"), order=idx + 1,
            )
            (running if idx == current else pending).append(entry)

    by_id = {book.abs_id: book for book in books}
    latest = {}
    for job in jobs:
        if job.abs_id not in by_id:
            continue
        key = (job.abs_id, job.kind or "alignment")
        earlier = latest.get(key)
        if earlier is None or (job.last_attempt or 0, job.id or 0) > (
                earlier.last_attempt or 0, earlier.id or 0):
            latest[key] = job

    for abs_id, book in by_id.items():
        title = book.abs_title or book.audio_title or abs_id
        status = book.status or ""
        alignment = latest.get((abs_id, "alignment"))
        epub_intent = bool(getattr(book, "readalong_epub_requested", False))
        alignment_pct = _percent(alignment.progress) if alignment else None
        if status in {"pending", "processing", "forging"}:
            (pending if status == "pending" else running).append(_entry(
                title, "Forge / Alignment", "queued" if status == "pending" else "running",
                detail={"pending": "Awaiting background processing",
                        "processing": "Aligning", "forging": "Forging"}.get(status),
                epub3=epub_intent, percent=alignment_pct,
                timestamp=getattr(alignment, "last_attempt", None),
            ))
        elif status in {"error", "failed_retry_later", "failed_permanent"}:
            recent.append(_entry(
                title, "Forge / Alignment", "failed",
                detail=(getattr(alignment, "last_error", None) or status),
                epub3=epub_intent, percent=alignment_pct,
                timestamp=getattr(alignment, "last_attempt", None),
            ))
        elif (alignment and (alignment.progress or 0) >= 1.0
              and (alignment.last_attempt or 0) >= now - 7 * 86400):
            recent.append(_entry(
                title, "Forge / Alignment", "completed", detail="Alignment completed",
                percent=alignment_pct, timestamp=alignment.last_attempt,
            ))

        readalong = latest.get((abs_id, "readalong"))
        if epub_intent:
            pending.append(_entry(
                title, "EPUB3 Read-along", "awaiting alignment",
                detail="Requested; generation starts after alignment finishes",
                epub3=True,
            ))
        if readalong is not None:
            progress = _percent(readalong.progress)
            stamp = readalong.last_attempt
            if (readalong.progress or 0) >= 1.0:
                state = "completed"
            elif readalong.last_error:
                state = "failed"
            elif abs_id in active_readalong_ids:
                state = "running"
            else:
                state = "interrupted"
            if state == "running" or (stamp or 0) >= now - 7 * 86400:
                detail = (readalong.last_error if state in {"failed", "completed"} and readalong.last_error
                          else readalong.stage or state.capitalize())
                (running if state == "running" else recent).append(_entry(
                    title, "EPUB3 Read-along", state, detail=detail, epub3=True,
                    percent=progress, timestamp=stamp,
                ))

    recent.sort(key=lambda row: row["timestamp"] or 0, reverse=True)
    return {"pending": pending, "running": running, "recent": recent[:25]}
