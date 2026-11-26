"""C35 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _get_stats_timezone():
    web = _web_server()
    tz_name = web.os.environ.get("TZ", "America/New_York") or "America/New_York"
    try:
        return web.ZoneInfo(tz_name)
    except Exception:
        web.get_persistent_condition_logger().warn(
            web.logger,
            f"invalid-tz:{tz_name}",
            "Invalid TZ '%s' for stats, falling back to America/New_York",
            tz_name,
            exc_info=True,
        )
        return web.ZoneInfo("America/New_York")


def _build_stats_cache_key():
    web = _web_server()
    try:
        user = web.current_user()
    except RuntimeError:
        user = None
    user_id = user.id if user is not None else "anon"
    return f"stats::{user_id}::{web.os.environ.get('TZ', 'America/New_York')}"


def _read_cached_stats():
    web = _web_server()
    cache_key = _build_stats_cache_key()
    with web.STATS_CACHE_LOCK:
        entry = web.STATS_CACHE.get(cache_key)
        if not entry:
            return None
        if (web.time.time() - entry["created_at"]) > web.STATS_CACHE_TTL_SECONDS:
            web.STATS_CACHE.pop(cache_key, None)
            return None
        return entry["payload"]


def _write_cached_stats(payload):
    web = _web_server()
    cache_key = _build_stats_cache_key()
    with web.STATS_CACHE_LOCK:
        web.STATS_CACHE[cache_key] = {
            "created_at": web.time.time(),
            "payload": payload,
        }


def _date_series(start_date, end_date):
    web = _web_server()
    values = []
    cursor = start_date
    while cursor <= end_date:
        values.append(cursor)
        cursor += web.timedelta(days=1)
    return values


def _activity_dates_from_daily(daily):
    web = _web_server()
    dates = set()
    for row in daily or []:
        try:
            if int(row.get("seconds") or 0) > 0:
                dates.add(web.datetime.fromisoformat(row["date"]).date())
        except Exception:
            continue
    return dates


def _calculate_current_streak_from_dates(activity_dates, reference_date):
    web = _web_server()
    streak = 0
    cursor = reference_date
    while cursor in activity_dates:
        streak += 1
        cursor -= web.timedelta(days=1)
    return streak


def _normalize_abs_author(metadata):
    authors = (metadata or {}).get("authors") or []
    names = [author.get("name") for author in authors if isinstance(author, dict) and author.get("name")]
    return ", ".join(names)


def _recent_daily_from_mapping(days_map, tz, days=7):
    web = _web_server()
    end_date = web.datetime.now(tz).date()
    start_date = end_date - web.timedelta(days=max(days - 1, 0))
    days_map = days_map if isinstance(days_map, dict) else {}
    buckets = {
        str(key): int(round(float(value or 0)))
        for key, value in days_map.items()
    }
    return [
        {
            "date": day.isoformat(),
            "seconds": buckets.get(day.isoformat(), 0),
        }
        for day in _date_series(start_date, end_date)
    ]


def _heatmap_window(today):
    """First and last date the stats heatmap can show."""
    web = _web_server()
    return today - web.timedelta(days=web._HEATMAP_DAYS), today


def _heatmap_from_mapping(days_map, start_date, end_date):
    days_map = days_map if isinstance(days_map, dict) else {}
    first, last = start_date.isoformat(), end_date.isoformat()
    heatmap = []
    for key, value in days_map.items():
        if first <= str(key)[:10] <= last:
            heatmap.append({
                "date": str(key),
                "seconds": int(round(float(value or 0))),
            })
    heatmap.sort(key=lambda row: row["date"])
    return heatmap


def _summarize_activity(daily, total_seconds, total_days, reference_date):
    daily = daily or []
    week_total = sum(int(row.get("seconds") or 0) for row in daily)
    best_day = max(daily, key=lambda row: int(row.get("seconds") or 0), default=None)
    activity_dates = _activity_dates_from_daily(daily)
    return {
        "totalSeconds": int(total_seconds or 0),
        "totalDays": int(total_days or 0),
        "weekTotalSeconds": week_total,
        "dailyAverageSeconds": int(week_total / max(len(daily), 1)),
        "bestDay": best_day,
        "currentStreakDays": _calculate_current_streak_from_dates(activity_dates, reference_date),
    }


def _normalize_listening_session(session_data):
    metadata = session_data.get("mediaMetadata") or {}
    started_at = int((session_data.get("startedAt") or 0) / 1000)
    ended_at = int((session_data.get("updatedAt") or 0) / 1000) or started_at
    return {
        "id": session_data.get("id"),
        "activityType": "listening",
        "absId": session_data.get("libraryItemId"),
        "title": session_data.get("displayTitle") or metadata.get("title") or "Unknown title",
        "subtitle": metadata.get("subtitle"),
        "author": session_data.get("displayAuthor") or _normalize_abs_author(metadata),
        "durationSeconds": int(round(float(session_data.get("timeListening") or 0))),
        "startedAt": started_at,
        "endedAt": ended_at,
        "coverPath": session_data.get("coverPath"),
    }


def _years_from_days(all_days):
    years = set()
    for key in (all_days or {}):
        try:
            years.add(int(str(key).split("-")[0]))
        except (IndexError, ValueError):
            pass
    return sorted(years)


def _abs_finished_books(progress, items, tz, abs_client=None):
    """Books ABS marks finished, newest first, with titles from the listening stats items."""
    web = _web_server()
    progress = progress if isinstance(progress, dict) else {}
    items = items if isinstance(items, dict) else {}
    titles = {}
    finished = []
    for item_id, entry in progress.items():
        # Podcast episodes have their own progress entries; only whole books count.
        if not isinstance(entry, dict) or not entry.get("isFinished") or entry.get("episodeId"):
            continue
        try:
            finished_dt = web.datetime.fromtimestamp(float(entry["finishedAt"]) / 1000, tz)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        details = None
        # ABS keeps ebook reading in the same progress records, with no duration. So does an
        # audiobook marked finished unplayed, so only the item says whether it has audio.
        if abs_client is not None and item_id not in items and not float(entry.get("duration") or 0) > 0:
            details = abs_client.get_item_details(item_id)
            media = (details or {}).get("media") or {}
            if details is not None and not (media.get("audioFiles") or float(media.get("duration") or 0) > 0):
                continue
        metadata = (items.get(item_id) or {}).get("mediaMetadata") or {}
        title = metadata.get("title")
        if not title:
            if not titles:
                titles = {book.abs_id: book.abs_title for book in web.database_service.get_all_books()}
            title = titles.get(item_id)
        authors = metadata.get("authors") or []
        # A book marked finished without a listening session: only ABS knows its title.
        if not title and abs_client is not None:
            if details is None:
                details = abs_client.get_item_details(item_id)
            metadata = ((details or {}).get("media") or {}).get("metadata") or {}
            title = metadata.get("title")
            authors = metadata.get("authors") or authors
        author = ", ".join(a.get("name") for a in authors if isinstance(a, dict) and a.get("name"))
        finished.append({
            "bookKey": f"abs:{item_id}", "absId": item_id,
            "title": title or "Unknown book", "author": author or None,
            "finishedAt": int(finished_dt.timestamp()),
            "finishedDate": finished_dt.date().isoformat(), "month": finished_dt.month,
        })
    finished.sort(key=lambda book: book["finishedAt"], reverse=True)
    return finished


def _build_listening_yearly_recap(all_days, year, finished_books, tz):
    """Year-in-review for listening: monthly hours from the ABS daily map, and the
    books ABS marks finished that year."""
    web = _web_server()
    months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
    finish_years = {
        book["absId"]: web.datetime.fromtimestamp(book["finishedAt"], tz).year for book in (finished_books or [])
    }
    finished_books = [book for book in (finished_books or []) if finish_years[book["absId"]] == year]
    for book in finished_books:
        months[book["month"] - 1]["finished"] += 1
    total_seconds = 0
    for key, value in (all_days or {}).items():
        date_str = str(key)
        if not date_str.startswith(f"{year}-"):
            continue
        try:
            month_index = int(date_str.split("-")[1]) - 1
        except (IndexError, ValueError):
            continue
        if month_index not in range(12):
            continue
        seconds = int(round(float(value or 0)))
        months[month_index]["seconds"] += seconds
        total_seconds += seconds
    return {
        "year": year,
        "months": months,
        "totalSeconds": total_seconds,
        "totalPages": 0,
        "booksFinished": len(finished_books),
        "finishedBooks": finished_books,
        # A book marked finished in a year with no listening time still needs its year listed.
        "availableYears": sorted(set(_years_from_days(all_days)) | set(finish_years.values())),
    }


def _build_listening_book_list():
    """Per-audiobook rollup from the bridge's own AUDIOBOOK reading sessions."""
    web = _web_server()
    stats_by_id = web.database_service.get_all_reading_stats()
    if not stats_by_id:
        return []
    books = {book.abs_id: book for book in web.database_service.get_all_books()}
    result = []
    for abs_id, stats in stats_by_id.items():
        listen_seconds = int(stats.get("listen_seconds") or 0)
        if listen_seconds <= 0:
            continue
        book = books.get(abs_id)
        listen_sessions = int(stats.get("listen_session_count") or 0)
        result.append({
            "bookKey": f"abs:{abs_id}",
            "absId": abs_id,
            "isLinked": True,
            "title": getattr(book, "abs_title", None) or stats.get("book_title") or "Unknown book",
            "author": None,
            "totalSeconds": listen_seconds,
            "sessionCount": listen_sessions,
            "avgSessionSeconds": listen_seconds // listen_sessions if listen_sessions else 0,
            "lastReadAt": int(stats["last_session_time"]) if stats.get("last_session_time") else None,
        })
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _abs_last_listened(abs_client, item_ids, per_page=100, max_pages=10):
    """Epoch seconds of each item's newest ABS listening session.

    ABS returns sessions newest first, so paging stops once every item has a date.
    Progress ``lastUpdate`` is no substitute: the bridge moves it when a reader leads.
    """
    wanted = set(item_ids or ())
    found = {}
    for page in range(max_pages):
        if not wanted - found.keys():
            break
        sessions = abs_client.get_listening_sessions(limit=per_page, page=page)
        if not isinstance(sessions, list):
            break
        for session in sessions:
            item_id = session.get("libraryItemId") if isinstance(session, dict) else None
            if item_id in wanted and item_id not in found and session.get("updatedAt"):
                found[item_id] = int(session["updatedAt"]) // 1000
        if len(sessions) < per_page:
            break
    return found


def _abs_listening_book_list(items, last_listened, titles):
    """Per-book rollup from ABS's own listening time, so the rows add up to its totals.

    ``last_listened`` maps item ids to epoch seconds and ``titles`` maps BookBridge
    abs_ids to titles. ABS gives no per-book session count.
    """
    last_listened = last_listened or {}
    result = []
    for item_id, item in (items or {}).items():
        total_seconds = int(round(float((item or {}).get("timeListening") or 0)))
        if total_seconds <= 0:
            continue
        metadata = item.get("mediaMetadata") or {}
        authors = metadata.get("authors") or []
        author = ", ".join(a.get("name") for a in authors if isinstance(a, dict) and a.get("name"))
        result.append({
            "bookKey": f"abs:{item_id}",
            "absId": item_id,
            "isLinked": item_id in titles,
            "title": metadata.get("title") or titles.get(item_id) or "Unknown book",
            "author": author or None,
            "totalSeconds": total_seconds,
            "sessionCount": None,
            "avgSessionSeconds": None,
            "lastReadAt": last_listened.get(item_id),
        })
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _abs_book_listening_detail(abs_client, abs_id, session_limit=40, max_pages=20):
    """One book's listening totals and newest-first sessions from ABS, or None if it has none.

    Matches the shape of ``get_book_listening_detail`` so the book detail can use either.
    """
    per_page = 100
    sessions = []
    for page in range(max_pages):
        data = abs_client.get_item_listening_sessions(abs_id, items_per_page=per_page, page=page)
        if not isinstance(data, dict):
            break
        batch = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]
        sessions.extend(batch)
        if len(batch) < per_page:
            break
    sessions = [s for s in sessions if float(s.get("timeListening") or 0) > 0]
    if not sessions:
        return None

    def fraction(session, key):
        duration = float(session.get("duration") or 0)
        return float(session.get(key) or 0) / duration if duration > 0 else None

    total_seconds = int(round(sum(float(s.get("timeListening") or 0) for s in sessions)))
    newest = sessions[0]
    newest_progress = fraction(newest, "currentTime")
    return {
        "totalSeconds": total_seconds,
        "sessionCount": len(sessions),
        "avgSessionSeconds": total_seconds // len(sessions),
        "progressPercent": round(min(newest_progress, 1.0) * 100, 1) if newest_progress is not None else None,
        "lastReadAt": int(newest.get("updatedAt") or 0) // 1000,
        "bookTitle": newest.get("displayTitle"),
        "sessions": [{
            "startTime": int(s.get("startedAt") or 0) // 1000,
            "endTime": int(s.get("updatedAt") or 0) // 1000,
            "durationSeconds": int(round(float(s.get("timeListening") or 0))),
            "startProgress": fraction(s, "startTime"),
            "endProgress": fraction(s, "currentTime"),
        } for s in sessions[:session_limit]],
    }


def _local_listening_stats(tz):
    """ABS-shaped listening stats from the bridge's own AUDIOBOOK sessions, or None if empty."""
    web = _web_server()
    days = web.database_service.get_listening_days(getattr(tz, "key", str(tz)))
    if not days:
        return None
    return {
        "days": days,
        "totalTime": sum(days.values()),
        "items": {
            abs_id: stats for abs_id, stats in web.database_service.get_all_reading_stats().items()
            if int(stats.get("listen_seconds") or 0) > 0
        },
    }


def _stats_abs_client():
    """The request user's own configured ABS client, or None. Never falls back to
    the global (primary admin) client when the user's bundle cannot be built."""
    web = _web_server()
    try:
        abs_client = web.container.user_client_registry().get_clients(web.current_user().id).abs_client
        return abs_client if abs_client and abs_client.is_configured() else None
    except Exception as e:
        web.logger.warning("Stats: user ABS client unavailable, using local sessions: %s", e, exc_info=True)
        return None


def _build_listening_stats_payload(tz):
    web = _web_server()
    abs_client = _stats_abs_client()
    use_abs = abs_client is not None
    if use_abs:
        raw_stats = abs_client.get_listening_stats()
    else:
        raw_stats = _local_listening_stats(tz)
    if not raw_stats:
        return None

    all_days = raw_stats.get("days") or {}
    daily = _recent_daily_from_mapping(all_days, tz, days=7)
    heatmap = _heatmap_from_mapping(all_days, *_heatmap_window(web.datetime.now(tz).date()))
    all_activity_dates = _activity_dates_from_daily([
        {"date": key, "seconds": value}
        for key, value in all_days.items()
    ])

    if use_abs:
        recent_sessions = raw_stats.get("recentSessions")
        if not isinstance(recent_sessions, list) or not recent_sessions:
            recent_sessions = abs_client.get_listening_sessions(limit=10)
        normalized_sessions = [
            _normalize_listening_session(session_data)
            for session_data in (recent_sessions or [])[:10]
            if isinstance(session_data, dict)
        ]
    else:
        normalized_sessions = web.database_service.get_recent_listening_sessions(10)

    summary = _summarize_activity(
        daily=daily,
        total_seconds=int(round(float(raw_stats.get("totalTime") or 0))),
        total_days=len(all_activity_dates),
        reference_date=web.datetime.now(tz).date(),
    )
    if use_abs:
        progress = abs_client.get_all_progress_raw()
        finished_books = _abs_finished_books(progress, raw_stats.get("items"), tz, abs_client)
        items = raw_stats.get("items") or {}
        books = _abs_listening_book_list(
            items, _abs_last_listened(abs_client, set(items)),
            {book.abs_id: book.abs_title for book in web.database_service.get_all_books()},
        )
    else:
        finished_books = web.database_service.get_listening_finished_books(getattr(tz, "key", str(tz)))
        books = _build_listening_book_list()
    summary["itemsFinished"] = len(finished_books)
    summary["daysListened"] = len(all_activity_dates)

    session_durations = [int(s.get("durationSeconds") or 0) for s in normalized_sessions if s.get("durationSeconds")]
    summary["avgSessionSeconds"] = int(sum(session_durations) / len(session_durations)) if session_durations else 0
    summary["hoursPerDay"] = round(summary.get("dailyAverageSeconds", 0) / 3600, 2)

    return {
        "available": True,
        "stats": summary,
        "daily": daily,
        "heatmap": heatmap,
        "recentSessions": normalized_sessions,
        "activityDates": [day.isoformat() for day in sorted(all_activity_dates)],
        "trackedBookIds": sorted({
            session.get("absId") for session in normalized_sessions if session.get("absId")
        }),
        "books": books,
        "yearlyRecap": _build_listening_yearly_recap(all_days, web.datetime.now(tz).year, finished_books, tz),
    }


def _build_reading_stats_payload(tz):
    web = _web_server()
    tz_name = getattr(tz, "key", str(tz))
    summary = web.database_service.get_koreader_dashboard_summary(tz_name)
    daily = web.database_service.get_koreader_daily_totals(7, tz_name)
    heatmap = web.database_service.get_koreader_heatmap(*_heatmap_window(web.datetime.now(tz).date()), tz_name)
    recent_sessions = web.database_service.get_koreader_recent_sessions(10, tz_name)
    activity_dates = web.database_service.get_koreader_activity_dates(tz_name)
    hour_histogram = web.database_service.get_koreader_hour_histogram(tz_name)
    books = web.database_service.get_koreader_book_list(tz_name)
    yearly_recap = web.database_service.get_koreader_yearly_recap(web.datetime.now(tz).year, tz_name)

    if not summary and not any(int(row.get("seconds") or 0) > 0 for row in daily):
        return {
            "available": False,
            "stats": None,
            "daily": daily,
            "heatmap": heatmap,
            "recentSessions": [],
            "activityDates": [],
            "trackedBookIds": [],
            "trackedBookKeys": [],
            "hourHistogram": hour_histogram,
            "books": [],
            "yearlyRecap": yearly_recap,
        }

    stats = summary or {}
    stats.setdefault("booksTracked", 0)
    stats.setdefault("linkedBooksTracked", 0)
    stats.setdefault("unlinkedBooksTracked", 0)
    stats.setdefault("daysRead", len(activity_dates))
    stats.setdefault("totalSeconds", 0)
    stats.setdefault("pagesRead", 0)
    stats.setdefault("weekTotalSeconds", sum(int(row.get("seconds") or 0) for row in daily))
    stats.setdefault("dailyAverageSeconds", int(stats["weekTotalSeconds"] / max(len(daily), 1)))
    stats.setdefault("bestDay", max(daily, key=lambda row: int(row.get("seconds") or 0), default=None))
    stats.setdefault("currentStreakDays", _calculate_current_streak_from_dates(
        {web.datetime.fromisoformat(day).date() for day in activity_dates},
        web.datetime.now(tz).date(),
    ))
    stats.setdefault("trackedBookIds", [])
    stats.setdefault("trackedBookKeys", [])

    return {
        "available": True,
        "stats": stats,
        "daily": daily,
        "heatmap": heatmap,
        "recentSessions": recent_sessions,
        "activityDates": activity_dates,
        "trackedBookIds": stats.get("trackedBookIds") or [],
        "trackedBookKeys": stats.get("trackedBookKeys") or [],
        "hourHistogram": hour_histogram,
        "books": books,
        "yearlyRecap": yearly_recap,
    }


def _merge_daily_activity(listening_daily, reading_daily, tz):
    web = _web_server()
    end_date = web.datetime.now(tz).date()
    start_date = end_date - web.timedelta(days=6)
    listening_map = {row["date"]: int(row.get("seconds") or 0) for row in listening_daily or []}
    reading_map = {row["date"]: int(row.get("seconds") or 0) for row in reading_daily or []}

    merged = []
    for day in _date_series(start_date, end_date):
        key = day.isoformat()
        listening_seconds = listening_map.get(key, 0)
        reading_seconds = reading_map.get(key, 0)
        merged.append({
            "date": key,
            "seconds": listening_seconds + reading_seconds,
            "listeningSeconds": listening_seconds,
            "readingSeconds": reading_seconds,
        })
    return merged


def _merge_heatmap_activity(listening_heatmap, reading_heatmap):
    web = _web_server()
    merged = web.defaultdict(lambda: {"seconds": 0, "listeningSeconds": 0, "readingSeconds": 0})
    for row in listening_heatmap or []:
        key = row["date"]
        value = int(row.get("seconds") or 0)
        merged[key]["seconds"] += value
        merged[key]["listeningSeconds"] += value
    for row in reading_heatmap or []:
        key = row["date"]
        value = int(row.get("seconds") or 0)
        merged[key]["seconds"] += value
        merged[key]["readingSeconds"] += value
    return [
        {"date": key, **values}
        for key, values in sorted(merged.items())
    ]


def _merge_recent_sessions(listening_sessions, reading_sessions, limit=10):
    merged = list(listening_sessions or []) + list(reading_sessions or [])
    merged.sort(key=lambda row: int(row.get("endedAt") or 0), reverse=True)
    return merged[: max(int(limit or 10), 1)]


def _merge_book_lists(reading_books, listening_books):
    """Union reading + listening per-book rows by bookKey (linked books merge)."""
    merged = {}
    for source, items in (("reading", reading_books), ("listening", listening_books)):
        for item in items or []:
            key = item.get("bookKey")
            if not key:
                continue
            entry = merged.setdefault(key, {
                "bookKey": key, "absId": item.get("absId"),
                "isLinked": bool(item.get("isLinked")),
                "title": item.get("title"), "author": item.get("author"),
                "readingSeconds": 0, "listeningSeconds": 0, "totalSeconds": 0,
                "pagesRead": 0, "lastReadAt": 0, "percentComplete": None,
            })
            seconds = int(item.get("totalSeconds") or 0)
            if source == "reading":
                entry["readingSeconds"] += seconds
                entry["pagesRead"] = item.get("pagesRead") or entry["pagesRead"]
                if item.get("percentComplete") is not None:
                    entry["percentComplete"] = item.get("percentComplete")
            else:
                entry["listeningSeconds"] += seconds
            entry["totalSeconds"] = entry["readingSeconds"] + entry["listeningSeconds"]
            entry["lastReadAt"] = max(int(entry["lastReadAt"] or 0), int(item.get("lastReadAt") or 0))
            if not entry.get("title") and item.get("title"):
                entry["title"] = item.get("title")

    result = list(merged.values())
    for entry in result:
        entry["lastReadAt"] = entry["lastReadAt"] or None
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _build_combined_yearly_recap(reading_recap, listening_recap):
    """Merge reading + listening monthly hours; finished timeline comes from reading."""
    reading_months = (reading_recap or {}).get("months") or []
    listening_months = (listening_recap or {}).get("months") or []
    months = []
    for index in range(12):
        rm = reading_months[index] if index < len(reading_months) else {}
        lm = listening_months[index] if index < len(listening_months) else {}
        read_secs = int(rm.get("seconds") or 0)
        listen_secs = int(lm.get("seconds") or 0)
        months.append({
            "month": index + 1,
            "seconds": read_secs + listen_secs,
            "readingSeconds": read_secs,
            "listeningSeconds": listen_secs,
            "pages": int(rm.get("pages") or 0),
            "finished": 0,
        })
    # A book finished as both an ebook and an audiobook counts once, at its first finish.
    finished_by_book = {}
    for book in list((reading_recap or {}).get("finishedBooks") or []) + list((listening_recap or {}).get("finishedBooks") or []):
        key = book.get("absId") or book.get("bookKey")
        if key not in finished_by_book or book["finishedAt"] < finished_by_book[key]["finishedAt"]:
            finished_by_book[key] = book
    finished_books = sorted(finished_by_book.values(), key=lambda book: book["finishedAt"], reverse=True)
    for book in finished_books:
        months[book["month"] - 1]["finished"] += 1
    available_years = sorted(
        set((reading_recap or {}).get("availableYears") or [])
        | set((listening_recap or {}).get("availableYears") or [])
    )
    return {
        "year": (reading_recap or listening_recap or {}).get("year"),
        "months": months,
        "totalSeconds": sum(month["seconds"] for month in months),
        "totalPages": (reading_recap or {}).get("totalPages") or 0,
        "booksFinished": len(finished_books),
        "finishedBooks": finished_books,
        "availableYears": available_years,
    }


def _build_combined_stats_payload(listening, reading, tz):
    web = _web_server()
    listening_daily = (listening or {}).get("daily") or []
    reading_daily = (reading or {}).get("daily") or []
    combined_daily = _merge_daily_activity(listening_daily, reading_daily, tz)
    combined_heatmap = _merge_heatmap_activity(
        (listening or {}).get("heatmap"),
        (reading or {}).get("heatmap"),
    )
    combined_sessions = _merge_recent_sessions(
        (listening or {}).get("recentSessions"),
        (reading or {}).get("recentSessions"),
        limit=10,
    )

    listening_dates = {
        web.datetime.fromisoformat(day).date()
        for day in ((listening or {}).get("activityDates") or [])
    }
    reading_dates = {
        web.datetime.fromisoformat(day).date()
        for day in ((reading or {}).get("activityDates") or [])
    }
    all_activity_dates = listening_dates | reading_dates

    listening_book_keys = {
        f"abs:{book_id}"
        for book_id in ((listening or {}).get("trackedBookIds") or [])
        if book_id
    }
    reading_book_keys = set((reading or {}).get("trackedBookKeys") or [])

    combined_stats = {
        "activeDays": len(all_activity_dates),
        "totalSeconds": int(((listening or {}).get("stats") or {}).get("totalSeconds") or 0)
        + int(((reading or {}).get("stats") or {}).get("totalSeconds") or 0),
        "booksWithActivity": len(
            listening_book_keys | reading_book_keys
        ),
        "weekTotalSeconds": sum(int(row.get("seconds") or 0) for row in combined_daily),
        "dailyAverageSeconds": int(
            sum(int(row.get("seconds") or 0) for row in combined_daily) / max(len(combined_daily), 1)
        ),
        "bestDay": max(combined_daily, key=lambda row: int(row.get("seconds") or 0), default=None),
        "currentStreakDays": _calculate_current_streak_from_dates(all_activity_dates, web.datetime.now(tz).date()),
    }

    return {
        "available": bool((listening and listening.get("available")) or (reading and reading.get("available"))),
        "stats": combined_stats if combined_stats["totalSeconds"] or combined_stats["activeDays"] else None,
        "daily": combined_daily,
        "heatmap": combined_heatmap,
        "recentSessions": combined_sessions,
        "hourHistogram": (reading or {}).get("hourHistogram") or [],
        "books": _merge_book_lists((reading or {}).get("books"), (listening or {}).get("books")),
        "yearlyRecap": _build_combined_yearly_recap(
            (reading or {}).get("yearlyRecap"),
            (listening or {}).get("yearlyRecap"),
        ),
    }


def _build_dashboard_progress_rows(books, all_states):
    """The per-book fields the dashboard's periodic refresh actually redraws.

    Derived from Book/State rows and scalar alignment status: no display-metadata
    resolution, no per-book service lookups, and above all no alignment map —
    which the full dashboard build loads per book to compute the drift badge
    (issue #412)."""
    web = _web_server()
    states_by_book = web._group_dashboard_states_by_book(all_states)
    ctc_aligned_book_ids = web.database_service.get_ctc_aligned_book_ids()
    readalong_ready_book_ids = web.database_service.get_readalong_ready_book_ids()
    sync_markers = web.database_service.get_sync_markers()
    rows = []

    for book in books or []:
        abs_id = getattr(book, "abs_id", None)
        if not abs_id:
            continue

        states = {}
        latest_update_time = 0
        max_progress = 0.0
        for state in states_by_book.get(abs_id, []):
            if state.last_updated and state.last_updated > latest_update_time:
                latest_update_time = state.last_updated
            pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0
            states[state.client_name] = {
                "timestamp": state.timestamp or 0,
                "percentage": pct_val,
            }
            if state.percentage is not None:
                max_progress = max(max_progress, pct_val)

        last_slot_client, last_slot_unix = sync_markers.get(abs_id, (None, 0))
        rows.append({
            "abs_id": abs_id,
            "ctc_aligned": abs_id in ctc_aligned_book_ids,
            "readalong_ready": abs_id in readalong_ready_book_ids or web._has_linked_readalong(book),
            "readalong_generated": abs_id in readalong_ready_book_ids,
            "unified_progress": min(max_progress, 100.0),
            "last_sync": web._format_dashboard_last_sync(latest_update_time),
            "last_sync_unix": latest_update_time,
            "last_slot": web._format_strip_slot(last_slot_client),
            "last_slot_client": last_slot_client,
            "last_slot_service": web._hero_leader_service(last_slot_client),
            "last_slot_unix": last_slot_unix,
            "last_slot_sync": web._format_dashboard_last_sync(last_slot_unix),
            "states": states,
        })

    return rows
