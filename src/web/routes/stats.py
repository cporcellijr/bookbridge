"""Stats HTTP routes registered without endpoint namespaces."""

from typing import Any

_WEB_MODULE = None


def _web_server():
    """Return the registering web module (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def register(app: Any, web_module: Any) -> None:
    """Register Stats endpoints without changing their Flask names."""
    global _WEB_MODULE
    _WEB_MODULE = web_module
    rules = (
        ("/stats", "stats_view", stats_view),
        ("/api/stats", "api_stats", api_stats),
        ("/api/stats/reading-day", "api_stats_reading_day", api_stats_reading_day),
        ("/api/stats/reading-calendar", "api_stats_reading_calendar", api_stats_reading_calendar),
        ("/api/stats/book-detail", "api_stats_book_detail", api_stats_book_detail),
        ("/api/stats/yearly-recap", "api_stats_yearly_recap", api_stats_yearly_recap),
    )
    for rule, endpoint, view in rules:
        app.add_url_rule(rule, endpoint, view)


def stats_view():
    web = _web_server()
    return web.render_template(web.ui_template('stats.html'))


def api_stats():
    web = _web_server()
    cached = web._read_cached_stats()
    if cached is not None:
        return web.jsonify(cached)

    tz = web._get_stats_timezone()
    listening = None
    reading = None

    try:
        listening = web._build_listening_stats_payload(tz)
    except Exception as e:
        web.logger.warning("Stats API: listening stats build failed: %s", e, exc_info=True)
        listening = None

    try:
        reading = web._build_reading_stats_payload(tz)
    except Exception as e:
        web.logger.warning("Stats API: reading stats build failed: %s", e, exc_info=True)
        reading = {
            "available": False,
            "stats": None,
            "daily": [],
            "heatmap": [],
            "recentSessions": [],
            "activityDates": [],
            "trackedBookIds": [],
            "trackedBookKeys": [],
        }

    combined = web._build_combined_stats_payload(
        listening or {"available": False, "stats": None, "daily": [], "heatmap": [], "recentSessions": []},
        reading,
        tz,
    )

    response = {
        "listening": listening,
        "reading": {
            "available": reading.get("available"),
            "stats": reading.get("stats"),
            "daily": reading.get("daily"),
            "heatmap": reading.get("heatmap"),
            "recentSessions": reading.get("recentSessions"),
            "trackedBookIds": reading.get("trackedBookIds"),
            "trackedBookKeys": reading.get("trackedBookKeys"),
            "hourHistogram": reading.get("hourHistogram") or [],
            "books": reading.get("books") or [],
            "yearlyRecap": reading.get("yearlyRecap"),
        } if reading else {
            "available": False,
            "stats": None,
            "daily": [],
            "heatmap": [],
            "recentSessions": [],
            "trackedBookIds": [],
            "trackedBookKeys": [],
            "hourHistogram": [],
            "books": [],
            "yearlyRecap": None,
        },
        "combined": combined,
    }
    web._write_cached_stats(response)
    return web.jsonify(response)


def api_stats_reading_day():
    web = _web_server()
    date_str = str(web.request.args.get("date") or "").strip()
    if not date_str:
        return web.jsonify({"error": "Missing date"}), 400

    try:
        target_date = web.datetime.fromisoformat(date_str).date()
    except ValueError:
        return web.jsonify({"error": "Invalid date format"}), 400

    try:
        tz = web._get_stats_timezone()
        payload = web.database_service.get_koreader_books_for_date(
            target_date.isoformat(),
            getattr(tz, "key", str(tz)),
        )
    except Exception as e:
        web.logger.warning("Stats API: reading day drilldown failed for %s: %s", date_str, e, exc_info=True)
        return web.jsonify({"error": "Failed to load reading day details"}), 500

    return web.jsonify(payload)


def api_stats_reading_calendar():
    web = _web_server()
    month_str = str(web.request.args.get("month") or "").strip()
    if not month_str:
        return web.jsonify({"error": "Missing month"}), 400

    try:
        web.datetime.fromisoformat(f"{month_str}-01")
    except ValueError:
        return web.jsonify({"error": "Invalid month format"}), 400

    try:
        tz = web._get_stats_timezone()
        payload = web.database_service.get_koreader_calendar_month(
            month_str,
            getattr(tz, "key", str(tz)),
        )
    except Exception as e:
        web.logger.warning("Stats API: reading calendar failed for %s: %s", month_str, e, exc_info=True)
        return web.jsonify({"error": "Failed to load reading calendar"}), 500

    return web.jsonify(payload)


def api_stats_book_detail():
    web = _web_server()
    key = str(web.request.args.get("key") or "").strip()
    if not key:
        return web.jsonify({"error": "Missing key"}), 400

    try:
        tz = web._get_stats_timezone()
        tz_name = getattr(tz, "key", str(tz))
        reading = web.database_service.get_koreader_book_detail(key, tz_name)

        abs_id = None
        if key.startswith("abs:"):
            abs_id = key.split("abs:", 1)[1]
        elif reading and reading.get("absId"):
            abs_id = reading.get("absId")

        listening = None
        if abs_id:
            # ABS holds the full listening history, including Storyteller-led time the
            # bridge credits there; the bridge's own sessions only cover what it saw.
            abs_client = web._stats_abs_client()
            detail = web._abs_book_listening_detail(abs_client, abs_id) if abs_client else None
            if detail is None:
                detail = web.database_service.get_book_listening_detail(abs_id)
            if detail and int(detail.get("totalSeconds") or 0) > 0:
                book = web.database_service.get_book(abs_id)
                snapshot_title = detail.pop("bookTitle", None)
                listening = {
                    "absId": abs_id,
                    "title": getattr(book, "abs_title", None) or snapshot_title,
                    **detail,
                }

        if not reading and not listening:
            return web.jsonify({"error": "No detail for this book"}), 404

        title = (reading or {}).get("title") or (listening or {}).get("title") or "Unknown book"
        return web.jsonify({
            "bookKey": key, "absId": abs_id, "title": title,
            "reading": reading, "listening": listening,
        })
    except Exception as e:
        web.logger.warning("Stats API: book detail failed for %s: %s", key, e, exc_info=True)
        return web.jsonify({"error": "Failed to load book detail"}), 500


def api_stats_yearly_recap():
    web = _web_server()
    scope = str(web.request.args.get("scope") or "combined").strip().lower()
    year_str = str(web.request.args.get("year") or "").strip()

    tz = web._get_stats_timezone()
    tz_name = getattr(tz, "key", str(tz))
    try:
        year = int(year_str) if year_str else web.datetime.now(tz).year
    except ValueError:
        return web.jsonify({"error": "Invalid year"}), 400

    try:
        # Each recap carries that calendar year's heatmap, so the page can show it.
        first_day, last_day = web.datetime(year, 1, 1).date(), web.datetime(year, 12, 31).date()
        reading_recap = web.database_service.get_koreader_yearly_recap(year, tz_name)
        reading_heatmap = web.database_service.get_koreader_heatmap(first_day, last_day, tz_name)

        listening_recap = None
        listening_heatmap = []
        finished_books = []
        try:
            abs_client = web._stats_abs_client()
            if abs_client:
                raw_stats = abs_client.get_listening_stats()
                if raw_stats:
                    finished_books = web._abs_finished_books(
                        abs_client.get_all_progress_raw(), raw_stats.get("items"), tz, abs_client
                    )
            else:
                raw_stats = web._local_listening_stats(tz)
                finished_books = web.database_service.get_listening_finished_books(tz_name)
        except Exception:
            raw_stats = None
        if raw_stats:
            listening_recap = web._build_listening_yearly_recap(
                raw_stats.get("days") or {}, year, finished_books, tz
            )
            listening_heatmap = web._heatmap_from_mapping(raw_stats.get("days") or {}, first_day, last_day)

        if scope == "reading":
            return web.jsonify({**reading_recap, "heatmap": reading_heatmap})
        if scope == "listening":
            listening = listening_recap or web._build_listening_yearly_recap({}, year, [], tz)
            return web.jsonify({**listening, "heatmap": listening_heatmap})
        combined = web._build_combined_yearly_recap(reading_recap, listening_recap)
        return web.jsonify({**combined, "heatmap": web._merge_heatmap_activity(listening_heatmap, reading_heatmap)})
    except Exception as e:
        web.logger.warning("Stats API: yearly recap failed for %s/%s: %s", scope, year, e, exc_info=True)
        return web.jsonify({"error": "Failed to load yearly recap"}), 500
