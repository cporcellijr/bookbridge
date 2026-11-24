"""Alignments HTTP routes registered without endpoint namespaces."""

from typing import Any


_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Alignments endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ('/api/alignments/llm-status', 'alignments_llm_status', alignments_llm_status, ['GET']),
        ('/api/alignments/realign', 'alignments_realign', alignments_realign, ['POST']),
        ('/api/alignments/restore', 'alignments_restore', alignments_restore, ['POST']),
    )
    for rule, endpoint, view, methods in rules:
        if methods is None:
            app.add_url_rule(rule, endpoint, view)
        else:
            app.add_url_rule(rule, endpoint, view, methods=methods)



def alignments_llm_status():
    """API: Report how each stored alignment map was built (which used the LLM)."""
    web = _web_server()
    try:
        # Self-heal legacy maps: classify NULL provenance by map shape (no re-transcription)
        # so the report and the re-align target list are accurate.
        web.database_service.backfill_alignment_methods()
        # Score maps stored before quality tracking existed, a bounded batch per call
        # so the health panel's scores fill in over a few page loads (issue #426 phase 4).
        web.database_service.backfill_alignment_quality()
        return web.jsonify(web.database_service.get_alignment_provenance())
    except Exception as e:
        web.logger.error(f"❌ Failed to read alignment provenance: {e}", exc_info=True)
        return web.jsonify({"error": str(e)}), 500


def alignments_realign():
    """API: Queue alignment maps for re-processing under the LLM-enabled pipeline.

    Body: {"abs_id": "..."} for one book, or {"scope": "all_non_llm"} to queue every
    pre-LLM/linear map. Sets the books' status to 'pending' so the forge pipeline
    rebuilds them on the next cycle.
    """
    web = _web_server()
    data = web.request.get_json(silent=True) or {}
    abs_id = (data.get("abs_id") or "").strip()
    scope = (data.get("scope") or "").strip()

    try:
        if abs_id:
            targets = [abs_id]
        elif scope == "all_non_llm":
            targets = web.database_service.get_books_needing_llm_realign()
        else:
            return web.jsonify({"error": "Provide 'abs_id' or scope 'all_non_llm'"}), 400

        queued = 0
        for target in targets:
            if web.database_service.set_book_status(target, "pending"):
                queued += 1
        web.logger.info(f"🔁 Re-align queued {queued} book(s) (scope='{scope or 'single'}')")
        return web.jsonify({"queued": queued})
    except Exception as e:
        web.logger.error(f"❌ Failed to queue re-align: {e}", exc_info=True)
        return web.jsonify({"error": str(e)}), 500


def alignments_restore():
    """API: Restore a book's alignment map to the backup taken before its last
    overwrite (issue #426 phase 4).

    Body: {"abs_id": "..."}. Returns {"restored": bool} — False when there is no
    backup on file for that book.
    """
    web = _web_server()
    data = web.request.get_json(silent=True) or {}
    abs_id = (data.get("abs_id") or "").strip()
    if not abs_id:
        return web.jsonify({"error": "Provide 'abs_id'"}), 400

    try:
        alignment_service = getattr(web.manager, "alignment_service", None) if web.manager else None
        restored = bool(alignment_service and alignment_service.restore_previous_alignment(abs_id))
        return web.jsonify({"restored": restored})
    except Exception as e:
        web.logger.error(f"❌ Failed to restore alignment for {abs_id}: {e}", exc_info=True)
        return web.jsonify({"error": str(e)}), 500
