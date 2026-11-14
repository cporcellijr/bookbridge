"""C24 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


# ---------------- ROUTES ----------------
def _dashboard_mappings_for_user(user) -> tuple[list, float, dict]:
    """Mappings, overall progress and integration flags for one user's dashboard."""
    web = _web_server()
    user_id = user.id if user else None
    books = web.database_service.get_all_books(user_id=user_id)
    all_states = web.database_service.get_all_states(
        user_id=user_id
    )
    books = web._dashboard_visible_books_for_user(books, user)
    all_hardcover = web.database_service.get_all_hardcover_details()
    all_storygraph = web.database_service.get_all_storygraph_details()
    all_goodreads = web.database_service.get_all_goodreads_details()
    all_reading_stats = web.database_service.get_all_reading_stats(user_id=user_id)
    cached_booklore_by_filename = web._index_cached_booklore_books(web.database_service.get_all_booklore_books())
    claim_times_by_book = web.database_service.get_book_claim_times(user_id=user_id)
    integrations = web._build_dashboard_integrations()
    mappings, overall_progress = web._build_dashboard_mappings(
        books,
        all_states,
        integrations,
        all_hardcover=all_hardcover,
        all_storygraph=all_storygraph,
        all_goodreads=all_goodreads,
        reading_stats_by_book=all_reading_stats,
        cached_booklore_by_filename=cached_booklore_by_filename,
        claim_times_by_book=claim_times_by_book,
    )
    return mappings, overall_progress, integrations


def _warm_dashboard_sync_warnings() -> None:
    """Build every user's dashboard mappings once at startup.

    The drift memo is empty after a restart and each audio-axis book then parses its
    whole alignment map, so the first dashboard load would pay for all of them.
    Running the real mapping build here fills the memo for the current positions.
    """
    web = _web_server()
    registry = web.container.user_client_registry()
    for user in web.database_service.list_users():
        user_token = credentials_token = None
        try:
            bundle = registry.get_clients(user.id)
            user_token = web.set_current_user_id(user.id)
            credentials_token = web.set_current_user_credentials(getattr(bundle, 'credentials', None))
            mappings, _progress, _integrations = _dashboard_mappings_for_user(user)
            web.logger.info("Dashboard warm-up built %d mapping(s) for user %s", len(mappings), user.id)
        except Exception as exc:
            web.logger.warning("Dashboard warm-up failed for user %s: %s", user.id, exc, exc_info=True)
        finally:
            if credentials_token is not None:
                web.reset_current_user_credentials(credentials_token)
            if user_token is not None:
                web.reset_current_user_id(user_token)


def _pick_hero_mapping(mappings: list[dict]) -> dict | None:
    """Select the most recently propagated in-progress book, deterministically."""
    eligible = [
        mapping for mapping in mappings
        if 0 < float(mapping.get("unified_progress") or 0) < 100
        and any(float(state.get("percentage") or 0) > 0
                for state in (mapping.get("states") or {}).values())
    ]
    return max(
        eligible,
        key=lambda mapping: (
            float(mapping.get("last_sync_leader_at") or 0),
            float(mapping.get("last_sync_unix") or 0),
            str(mapping.get("abs_id") or ""),
        ),
        default=None,
    )


def _hero_leader_service(leader: str | None) -> str | None:
    """Display the exact service that led the latest propagated sync."""
    web = _web_server()
    if not leader:
        return None
    key = str(leader).strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "KOReader"
    return web._FORMAT_STRIP_SERVICE_NAMES.get(key)


def _hero_leader_percent(mapping: dict, leader: str | None) -> float | None:
    """Use the specific leader's position when it has an owned progress row."""
    key = str(leader or "").strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        key = "kosync"
    return (mapping.get("states") or {}).get(key, {}).get("percentage")


def _hero_plain_text(value: object) -> str:
    """Strip markup and scripts from untrusted source metadata."""
    web = _web_server()
    if not isinstance(value, str):
        return ""
    safe = web.re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", value, flags=web.re.I | web.re.S)
    return web.re.sub(r"\s+", " ", web.html.unescape(web.re.sub(r"<[^>]*>", " ", safe))).strip()[:12000]


def _hero_synopsis_source(book, user) -> str | None:
    """Use local EPUB, the reader's ABS metadata, then their BookOrbit detail."""
    web = _web_server()
    for filename in dict.fromkeys((book.original_ebook_filename, book.ebook_filename)):
        if not filename or not str(filename).lower().endswith(".epub"):
            continue
        path = web.find_ebook_file(filename)
        if path:
            description = _hero_plain_text(web.extract_epub_metadata(str(path)).get("summary"))
            if description:
                return description
    bundle = web.container.user_client_registry().get_clients(user.id)
    abs_client = getattr(bundle, "abs_client", None)
    if abs_client and abs_client.is_configured():
        info = abs_client.get_item_details(book.abs_id)
        if isinstance(info, dict):
            media = info.get("media") if isinstance(info.get("media"), dict) else {}
            metadata = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
            description = _hero_plain_text(
                metadata.get("description") or media.get("description") or info.get("description")
            )
            if description:
                return description
    orbit_id = None
    if (book.ebook_source or "").lower() == "bookorbit":
        orbit_id = book.ebook_source_id
    elif (book.audio_source or "").lower() in ("bookorbit", "bookorbitaudio"):
        orbit_id = book.audio_source_id
    orbit = getattr(bundle, "bookorbit_client", None)
    if orbit_id and orbit and orbit.is_configured():
        detail = orbit.get_book_detail(orbit_id)
        if isinstance(detail, dict):
            nested = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
            description = _hero_plain_text(
                detail.get("description") or detail.get("summary")
                or nested.get("description") or nested.get("summary")
            )
            if description:
                return description
    return None


def _hero_cached_synopsis(book, user) -> str | None:
    """Keep a found synopsis per reader for good; retry an empty lookup after a week."""
    web = _web_server()
    lock = web._HERO_SYNOPSIS_LOCKS[hash(book.abs_id) % len(web._HERO_SYNOPSIS_LOCKS)]
    with lock:
        checked_at, description = web.database_service.get_user_book_synopsis_cache(user.id, book.abs_id)
        if description or (checked_at and web.time.time() - checked_at < web._HERO_SYNOPSIS_RECHECK_SECONDS):
            return description
        description = _hero_synopsis_source(book, user)
        web.database_service.set_user_book_synopsis_cache(user.id, book.abs_id, description, web.time.time())
        return description


def _hero_daily_totals(rows: list[dict], count: int, field: str, tz) -> dict:
    """Return today/week only with a complete history, never a truncated sample."""
    web = _web_server()
    if not rows or count > len(rows):
        return {}
    today = web.datetime.now(tz).date()
    monday = today - web.timedelta(days=today.weekday())
    amounts = {"today": 0, "week": 0}
    for row in rows:
        try:
            when = row.get("date")
            date_value = (
                web.datetime.fromisoformat(when).date() if when
                else web.datetime.fromtimestamp(float(row["endTime"]), tz).date()
            )
            value = int(row.get(field) or 0)
        except (KeyError, TypeError, ValueError, OSError):
            return {}
        if monday <= date_value <= today:
            amounts["week"] += value
        if date_value == today:
            amounts["today"] += value
    return amounts


def _hero_listening_piece(book, abs_id: str) -> dict | None:
    """Per-reader duration totals, with optional local day/week breakdown."""
    web = _web_server()
    abs_client = web._stats_abs_client()
    detail = web._abs_book_listening_detail(abs_client, abs_id) if abs_client else None
    if detail is None:
        detail = web.database_service.get_book_listening_detail(abs_id)
    if not detail or int(detail.get("totalSeconds") or 0) <= 0:
        return None
    payload = {"seconds": int(detail["totalSeconds"])}
    duration = float(book.duration or book.audio_duration or 0)
    if duration > 0:
        payload["lengthSeconds"] = round(duration)
        if detail.get("progressPercent") is not None:
            progress = min(100, max(0, float(detail["progressPercent"])))
            payload["leftSeconds"] = max(0, round(duration * (1 - progress / 100)))
    payload.update(_hero_daily_totals(
        detail.get("sessions") or [], int(detail.get("sessionCount") or 0),
        "durationSeconds", web._get_stats_timezone(),
    ))
    return payload


def _hero_reading_piece(abs_id: str) -> dict | None:
    """Per-reader KOReader pages, enriched with catalog page totals where known."""
    web = _web_server()
    tz = web._get_stats_timezone()
    reading = web.database_service.get_koreader_book_detail(
        f"abs:{abs_id}", getattr(tz, "key", str(tz)),
    )
    if not reading or int(reading.get("pagesRead") or 0) <= 0:
        return None
    payload = {"read": int(reading["pagesRead"])}
    storygraph = web.database_service.get_storygraph_details(abs_id)
    hardcover = web.database_service.get_hardcover_details(abs_id)
    total = (
        getattr(storygraph, "storygraph_pages", None)
        or getattr(hardcover, "hardcover_pages", None)
    )
    if total and int(total) > 0:
        payload["total"] = int(total)
        payload["left"] = max(0, int(total) - payload["read"])
    heatmap = reading.get("heatmap") or []
    payload.update(_hero_daily_totals(heatmap, len(heatmap), "pages", tz))
    return payload
