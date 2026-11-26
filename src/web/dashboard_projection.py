"""C21 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _format_dashboard_last_sync(latest_update_time):
    web = _web_server()
    if latest_update_time <= 0:
        return "Never"
    diff = web.time.time() - latest_update_time
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    return f"{int(diff // 3600)}h ago"


def _build_dashboard_integrations():
    web = _web_server()
    integrations = {}
    sync_clients = web.uc().sync_clients
    client_items = sync_clients.items() if hasattr(sync_clients, "items") else sync_clients
    try:
        iterator = list(client_items)
    except TypeError:
        return integrations
    for client_name, client in iterator:
        integrations[client_name.lower()] = bool(client.is_configured())
    return integrations


def _group_dashboard_states_by_book(all_states):
    states_by_book = {}
    for state in all_states or []:
        states_by_book.setdefault(state.abs_id, []).append(state)
    return states_by_book


def _dashboard_leader_service(leader_client: str | None) -> str | None:
    """Map a raw ReadingSession.leader_client to the dashboard's service key.

    The returned key matches the lowercase client name used in ``mapping["states"]``
    and the per-service ``.service-item`` blocks in ``index.html`` (``'abs'``,
    ``'kosync'``, ``'storyteller'``, ``'booklore'``, ``'bookloreaudio'``,
    ``'bookorbit'``, ``'bookorbitaudio'``, ``'kavita'``, ``'cwa'``), so the 'In Progress' card can
    subtly dot whichever service last moved the position.

    This is display-only: internal client keys are never renamed. KoSync device
    variants (``KoSync:<device>``, ``BridgeSync_Plugin``) collapse to ``'kosync'``;
    ``ABSEbook`` shares the single ABS row; audio leaders keep their own row (the
    template renders distinct GR/BO Audio rows); unknown / write-only tracker
    leaders return None (no dot).
    """
    if not leader_client:
        return None
    key = str(leader_client).strip().lower()
    if not key:
        return None
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "kosync"
    if key in ("abs", "absebook"):
        return "abs"
    if key == "storyteller":
        return "storyteller"
    if key == "booklore":
        return "booklore"
    if key == "bookloreaudio":
        return "bookloreaudio"
    if key == "bookorbit":
        return "bookorbit"
    if key == "bookorbitaudio":
        return "bookorbitaudio"
    if key == "kavita":
        return "kavita"
    if key == "cwa":
        return "cwa"
    return None


def _format_strip_slot(leader_client: str | None) -> str | None:
    """Map a raw ReadingSession.leader_client to its library-card format slot.

    Unlike ``_dashboard_leader_service`` this keeps ABS ebook apart from ABS
    audio, because the two light different slots. Trackers return None."""
    web = _web_server()
    if not leader_client:
        return None
    key = str(leader_client).strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "koreader"
    if key in ("abs", "bookorbitaudio", "bookloreaudio"):
        return "audio"
    if key in web._FORMAT_STRIP_EBOOK_CLIENTS:
        return "ebook"
    if key == "storyteller":
        return "readalong"
    return None


def _build_format_strip(mapping: dict) -> list[dict]:
    """The library card's four format slots: ebook, audio, read-along, KOReader.

    Each slot says whether the book is linked there, the service and position
    standing for it, whether it led the last sync, and whether it is the
    position that disagrees on an out-of-sync book."""
    web = _web_server()
    states = mapping.get("states") or {}
    ebook_states = [(c, states[c]) for c in web._FORMAT_STRIP_EBOOK_CLIENTS if c in states]
    ebook_client, ebook_state = max(
        ebook_states, key=lambda item: item[1].get("last_updated") or 0, default=(None, None)
    )
    ebook_service = web._FORMAT_STRIP_SERVICE_NAMES.get(ebook_client or mapping.get("ebook_source") or "")
    if not ebook_service and mapping.get("booklore_id"):
        ebook_service = "Grimmory"
    if not ebook_service and mapping.get("bookfusion_linked"):
        ebook_service = "BookFusion"
    audio_client = {"BookOrbit": "bookorbitaudio", "BookLore": "bookloreaudio"}.get(mapping.get("audio_source"), "abs")
    storyteller_linked = bool(
        mapping.get("storyteller_uuid") or mapping.get("storyteller_legacy_link") or "storyteller" in states
    )
    rows = [
        ("ebook", bool(ebook_client or ebook_service), ebook_service, ebook_state),
        ("audio", mapping.get("sync_mode") != "ebook_only", web._FORMAT_STRIP_SERVICE_NAMES.get(audio_client), states.get(audio_client)),
        # service None = BookBridge's own generated read-along EPUB.
        ("readalong", storyteller_linked or bool(mapping.get("readalong_ready")),
         "Storyteller" if storyteller_linked else None, states.get("storyteller")),
        ("koreader", "kosync" in states, "KOReader", states.get("kosync")),
    ]
    last_slot = _format_strip_slot(mapping.get("last_sync_leader"))
    slots = [
        {
            "key": key,
            "linked": linked,
            "service": service,
            "pct": state.get("percentage") if linked and state else None,
            "last": linked and key == last_slot,
            "oos": False,
        }
        for key, linked, service, state in rows
    ]
    if mapping.get("is_out_of_sync"):
        # shortcut: compares raw percentages, so an audio slot (time axis) can be
        # picked on books with long front/back matter; use the text-axis values
        # from _compute_dashboard_sync_warning_pct if that shows up in practice.
        leader = next((s for s in slots if s["last"] and s["pct"]), None)
        reference = leader["pct"] if leader else mapping.get("unified_progress") or 0
        others = [s for s in slots if s["pct"] and s is not leader]
        if others:
            max(others, key=lambda s: abs(s["pct"] - reference))["oos"] = True
    return slots


def _browser_cover_url(
    raw_cover_url: str | None,
    audio_source: str | None = None,
    audio_source_id: str | None = None,
    abs_id: str | None = None,
    ebook_source: str | None = None,
    ebook_source_id: str | None = None,
) -> str:
    """Convert any cover URL into a safe, same-origin BookBridge URL.

    This is the single choke point that keeps source hostnames and API tokens
    out of the browser (issue #353). It also neutralizes legacy
    ``audio_cover_url`` values already saved in the database, so no migration
    is needed.

    Priority:
    1. If ``raw_cover_url`` is a non-empty same-origin relative path (starts
       with a single ``/`` but not ``//`` or ``/\\``), return it unchanged.
       Backslashes are rejected because browsers normalize them to forward
       slashes when resolving URLs, so ``/\\evil.example`` would resolve as a
       protocol-relative cross-origin URL.
    2. Otherwise derive a same-origin proxy route from the source:
       - BookLore with ``audio_source_id`` -> ``/api/booklore/audiobook-cover/<id>``
       - BookOrbit with ``audio_source_id`` -> ``/api/bookorbit/audiobook-cover/<id>``
       - ABS or unset source with ``abs_id`` (preferred) or ``audio_source_id`` ->
         ``/api/cover-proxy/<id>`` (only for non-library audio sources)
    3. Failing that, derive from the ebook library that hosts the book, so
       ebook-only mappings still get art: BookOrbit/BookLore with
       ``ebook_source_id`` reuse that provider's cover proxy (one endpoint
       serves a book's cover whether the book is audio or text). CWA, Kavita
       and local files expose no id we can proxy, so they stay coverless.
    4. If nothing can be derived, return an empty string.

    An ebook-only mapping has no Audiobookshelf item, so its synthetic
    ``ebook-<hash>`` key must not be turned into a cover-proxy URL that can only
    404. Legacy rows whose ``audio_source`` is unset but whose id is a real ABS
    item are still served, so the test is on the id, not the source.
    """
    web = _web_server()
    raw = (raw_cover_url or "").strip()
    if raw.startswith("/") and not raw.startswith("//") and not raw.startswith("/\\"):
        return raw

    source = (audio_source or "").strip()
    normalized_audio_source = web._normalize_text_source_type(source)
    src_id = (audio_source_id or "").strip()
    aid = (abs_id or "").strip()

    if normalized_audio_source == "Booklore" and src_id:
        return f"/api/booklore/audiobook-cover/{src_id}"
    if normalized_audio_source == "BookOrbit" and src_id:
        return f"/api/bookorbit/audiobook-cover/{src_id}"
    if normalized_audio_source not in ("Booklore", "BookOrbit"):
        proxy_id = aid or src_id
        if proxy_id and not _is_synthetic_bridge_key(proxy_id):
            return f"/api/cover-proxy/{proxy_id}"

    ebook_src = (ebook_source or "").strip()
    ebook_id = (ebook_source_id or "").strip()
    if ebook_id:
        normalized_ebook_source = web._normalize_text_source_type(ebook_src)
        if normalized_ebook_source == "Booklore":
            return f"/api/booklore/book-cover/{ebook_id}"
        if normalized_ebook_source == "BookOrbit":
            return f"/api/bookorbit/audiobook-cover/{ebook_id}"
    return ""


def _is_synthetic_bridge_key(candidate: str) -> bool:
    """True when an id is a bridge-minted key rather than an ABS item id.

    Ebook-only mappings use ``ebook-<kosync_doc_id[:16]>`` (and ``ebook:<key>``
    in the match queue); library audiobooks use ``booklore:``/``bookorbit:``.
    None of these can be fetched from Audiobookshelf.
    """
    web = _web_server()
    key = (candidate or "").strip().lower()
    if key.startswith(("ebook-", "ebook:")):
        return True
    return any(key.startswith(f"{prefix}:") for prefix in web._AUDIO_BRIDGE_PREFIXES)


def _sanitize_cover_urls(entries: list) -> list:
    """Return copies of suggestion/queue dicts with browser-safe cover URLs.

    Suggestion and match-queue entries can be restored from a scan cache
    persisted by an older build, so they may still carry tokenized source URLs
    (issue #353). The originals are left untouched because they are shared with
    the session state and the on-disk cache.
    """
    sanitized = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            sanitized.append(entry)
            continue
        keys = [key for key in ("audio_cover_url", "cover_url") if key in entry]
        if not keys:
            sanitized.append(entry)
            continue
        safe_cover = _browser_cover_url(
            entry.get("audio_cover_url") or entry.get("cover_url"),
            audio_source=entry.get("audio_source"),
            audio_source_id=entry.get("audio_source_id"),
            abs_id=entry.get("bridge_key") or entry.get("abs_id"),
        )
        copied = dict(entry)
        for key in keys:
            copied[key] = safe_cover
        sanitized.append(copied)
    return sanitized


def _public_link_base(web_url_key: str, server_fallback: str) -> str:
    """Browser-facing base URL for a service, falling back to its server URL.

    A service's server URL is what BookBridge calls, and on a Docker network that
    is routinely a name the browser cannot resolve (`audiobookshelf:80`). The
    optional `*_WEB_URL` setting is the address to send a browser to instead.
    Every link rendered for a user goes through here so a public URL can't be
    honoured on one link and silently ignored on another.
    """
    web = _web_server()
    return (web.os.environ.get(web_url_key, '') or '').strip().rstrip('/') or (server_fallback or '').rstrip('/')


def _prefetch_bookfusion_links(books: list, integrations: dict | None) -> dict:
    """Resolve every book's BookFusion link in one query instead of one per book.

    Returns ``{abs_id: link_dict}``, empty when BookFusion is unconfigured or no
    user resolves. The dashboard template gates the BookFusion tile on the same
    ``integrations['bookfusion']`` flag, so skipping the query when that flag is
    falsey changes nothing the user sees.
    """
    web = _web_server()
    if not integrations or not integrations.get('bookfusion'):
        return {}
    user_id = web._active_bookfusion_link_user_id()
    if user_id is None:
        return {}
    abs_ids = [
        abs_id for abs_id in (getattr(book, "abs_id", None) for book in (books or [])) if abs_id
    ]
    if not abs_ids:
        return {}
    try:
        return web.database_service.get_user_bookfusion_links_for_books(user_id, abs_ids) or {}
    except Exception as exc:
        web.logger.debug("BookFusion dashboard link prefetch failed: %s", exc, exc_info=True)
        return {}


def _bookorbit_source_ids(books: list) -> set:
    """BookOrbit book ids referenced by these mappings, either side."""
    ids = set()
    for book in books or []:
        for attr, source in (("ebook_source_id", "ebook_source"),
                             ("audio_source_id", "audio_source")):
            if getattr(book, source, None) == "BookOrbit":
                value = getattr(book, attr, None)
                if value:
                    ids.add(str(value))
    return ids


def _prefetch_bookorbit_authors(books: list, integrations: dict | None) -> dict:
    """Resolve BookOrbit's own author metadata in one bulk read.

    Returns ``{bookorbit_book_id: author}``, empty when BookOrbit is unconfigured
    or nothing in the library came from it.

    BookOrbit is the only author source for a BookOrbit-sourced ebook on an
    install without Grimmory: ``base_author`` is always empty here,
    ``_get_cached_ebook_display_metadata`` reads the Grimmory cache alone, and the
    filename fallback needs a literal ``Title - Author`` stem that BookOrbit's
    filenames ("03. Other Worlds Than These (2026).epub") do not use. The client
    keeps its own TTL'd light-info cache behind a non-blocking refresh lock, so
    this is a dict read once warm and a couple of paginated calls when cold.
    """
    web = _web_server()
    if not integrations or not integrations.get('bookorbit'):
        return {}
    wanted = _bookorbit_source_ids(books)
    if not wanted:
        return {}
    try:
        client = web.uc().bookorbit_client
        if not client or not client.is_configured():
            return {}
        authors = {}
        cached, stale = client.book_cache_snapshot()
        if cached:
            # Serve the last listing and refresh it off the request: an hourly
            # TTL expiry must not hold the page behind paginated provider calls.
            if stale:
                web._spawn_user_background(client.get_all_books, label="bookorbit-cache-refresh")
            catalog = cached
        else:
            catalog = client.get_all_books() or []
        for info in catalog:
            book_id = info.get("id")
            if book_id is None:
                continue
            key = str(book_id)
            if key not in wanted:
                continue
            author = web._normalize_dashboard_display_value(info.get("authors"))
            if author:
                authors[key] = author
        return authors
    except Exception as exc:
        web.logger.debug("BookOrbit dashboard author prefetch failed: %s", exc, exc_info=True)
        return {}


def _bookorbit_author_for_book(book, bookorbit_authors: dict | None) -> str:
    """The prefetched author for whichever side of this book BookOrbit supplies."""
    if not bookorbit_authors:
        return ""
    for attr, source in (("ebook_source_id", "ebook_source"),
                         ("audio_source_id", "audio_source")):
        if getattr(book, source, None) == "BookOrbit":
            value = getattr(book, attr, None)
            if value:
                found = bookorbit_authors.get(str(value))
                if found:
                    return found
    return ""


def _build_dashboard_mapping(
    book,
    states_by_book,
    integrations,
    hardcover_by_book,
    storygraph_by_book,
    reading_stats_by_book,
    cached_booklore_by_filename,
    claim_times_by_book=None,
    bookfusion_by_book=None,
    bookorbit_authors=None,
    goodreads_by_book=None,
):
    web = _web_server()
    states = states_by_book.get(book.abs_id, [])
    state_by_client = {state.client_name: state for state in states}

    display_meta = web._resolve_dashboard_display_metadata(
        book,
        getattr(book, "audio_title", None) or book.abs_title,
        "",
        "",
        cached_booklore_by_filename=cached_booklore_by_filename,
        storyteller_meta=web._get_cached_storyteller_display_metadata(book),
        bookorbit_author=_bookorbit_author_for_book(book, bookorbit_authors),
    )
    display_title = display_meta["display_title"]
    display_subtitle = display_meta["display_subtitle"]
    display_author = display_meta["display_author"]

    mapping = {
        "abs_id": book.abs_id,
        "abs_title": display_title,
        "abs_subtitle": display_subtitle,
        "abs_author": display_author,
        "display_title": display_title,
        "display_subtitle": display_subtitle,
        "display_author": display_author,
        "display_filename": display_meta["display_filename"],
        "audio_source": getattr(book, "audio_source", None) or ("ABS" if getattr(book, "sync_mode", "audiobook") != "ebook_only" else None),
        "audio_source_id": getattr(book, "audio_source_id", None) or book.abs_id,
        "audio_title": getattr(book, "audio_title", None) or display_title,
        "audio_duration": getattr(book, "audio_duration", None) or book.duration or 0,
        "audio_cover_url": getattr(book, "audio_cover_url", None),
        "ebook_filename": book.ebook_filename,
        "original_ebook_filename": getattr(book, "original_ebook_filename", None),
        "has_local_epub": bool(book.original_ebook_filename or book.ebook_filename),
        "ebook_source": getattr(book, "ebook_source", None),
        "ebook_source_id": getattr(book, "ebook_source_id", None),
        "kosync_doc_id": book.kosync_doc_id,
        "transcript_file": book.transcript_file,
        "status": book.status,
        "sync_mode": getattr(book, "sync_mode", "audiobook"),
        "unified_progress": 0,
        "duration": book.duration or 0,
        # A Spines-view height that stays with the book under any filter or sort (crc32: stable across processes).
        "spine_h": (196, 204, 214, 224)[web.zlib.crc32(book.abs_id.encode("utf-8")) % 4],
        "storyteller_uuid": book.storyteller_uuid,
        "added_at_unix": (claim_times_by_book or {}).get(book.abs_id, 0.0),
        "states": {},
    }

    if book.status in ("processing", "forging", "failed_retry_later", "failed_permanent", "error"):
        job = web.database_service.get_latest_job(book.abs_id, kind=web.JOB_KIND_ALIGNMENT)
        if job:
            mapping["job_progress"] = round((job.progress or 0.0) * 100, 1)
            mapping["job_last_error"] = job.last_error
        else:
            mapping["job_progress"] = 0.0

    latest_update_time = 0
    max_progress = 0
    for client_name, state in state_by_client.items():
        if state.last_updated and state.last_updated > latest_update_time:
            latest_update_time = state.last_updated

        pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0
        mapping["states"][client_name] = {
            "timestamp": state.timestamp or 0,
            "percentage": pct_val,
            "last_updated": state.last_updated,
            "xpath": getattr(state, "xpath", None),
        }
        if getattr(state, "cfi", None) is not None:
            mapping["states"][client_name]["cfi"] = getattr(state, "cfi", None)

        if state.percentage is not None:
            max_progress = max(max_progress, pct_val)

        if client_name == "kosync":
            mapping["kosync_pct"] = pct_val
            mapping["kosync_xpath"] = getattr(state, "xpath", None)
        elif client_name == "abs":
            mapping["abs_pct"] = pct_val
            mapping["abs_ts"] = state.timestamp
        elif client_name == "storyteller":
            mapping["storyteller_pct"] = pct_val
            mapping["storyteller_xpath"] = getattr(state, "xpath", None)
        elif client_name == "booklore":
            mapping["booklore_pct"] = pct_val
            mapping["booklore_xpath"] = getattr(state, "xpath", None)

    hardcover_details = hardcover_by_book.get(book.abs_id)
    if hardcover_details:
        mapping.update({
            "hardcover_book_id": hardcover_details.hardcover_book_id,
            "hardcover_slug": hardcover_details.hardcover_slug,
            "hardcover_edition_id": hardcover_details.hardcover_edition_id,
            "hardcover_pages": hardcover_details.hardcover_pages,
            "isbn": hardcover_details.isbn,
            "asin": hardcover_details.asin,
            "matched_by": hardcover_details.matched_by,
            "hardcover_linked": True,
            "hardcover_title": book.abs_title,
        })
    else:
        mapping.update({
            "hardcover_book_id": None,
            "hardcover_slug": None,
            "hardcover_edition_id": None,
            "hardcover_pages": None,
            "isbn": None,
            "asin": None,
            "matched_by": None,
            "hardcover_linked": False,
            "hardcover_title": None,
        })

    storygraph_details = storygraph_by_book.get(book.abs_id)
    if storygraph_details:
        mapping.update({
            "storygraph_book_id": storygraph_details.storygraph_book_id,
            "storygraph_linked": True,
            "storygraph_url": storygraph_details.storygraph_url,
            "storygraph_title": book.abs_title,
            "storygraph_matched_by": storygraph_details.matched_by,
            "storygraph_rating": web._coerce_dashboard_rating(getattr(storygraph_details, "storygraph_rating", None)),
            "storygraph_review_count": web._coerce_dashboard_count(getattr(storygraph_details, "storygraph_review_count", None)),
            "storygraph_pages": storygraph_details.storygraph_pages,
        })
    else:
        mapping.update({
            "storygraph_book_id": None,
            "storygraph_linked": False,
            "storygraph_url": None,
            "storygraph_title": None,
            "storygraph_matched_by": None,
            "storygraph_rating": None,
            "storygraph_review_count": None,
            "storygraph_pages": None,
        })

    goodreads_details = (goodreads_by_book or {}).get(book.abs_id)
    mapping.update({
        "goodreads_book_id": goodreads_details.goodreads_book_id if goodreads_details else None,
        "goodreads_linked": bool(goodreads_details),
        "goodreads_url": goodreads_details.goodreads_url if goodreads_details else None,
        "goodreads_title": book.abs_title if goodreads_details else None,
        "goodreads_matched_by": goodreads_details.matched_by if goodreads_details else None,
        "goodreads_rating": web._coerce_dashboard_rating(goodreads_details.goodreads_rating) if goodreads_details else None,
        "goodreads_review_count": web._coerce_dashboard_count(goodreads_details.goodreads_review_count) if goodreads_details else None,
    })

    bookfusion_link = (bookfusion_by_book or {}).get(book.abs_id)
    if not isinstance(bookfusion_link, dict):
        bookfusion_link = None
    mapping.update({
        "bookfusion_id": (bookfusion_link or {}).get("bookfusion_id"),
        "bookfusion_title": (bookfusion_link or {}).get("title"),
        "bookfusion_linked": bool(bookfusion_link),
    })

    mapping["storyteller_legacy_link"] = "storyteller" in state_by_client and not book.storyteller_uuid

    if mapping.get("sync_mode") == "ebook_only":
        mapping["abs_url"] = None
        mapping["audio_url"] = None
    elif mapping["audio_source"] == "BookLore":
        mapping["abs_url"] = None
        _bl_audio_base = _public_link_base('BOOKLORE_WEB_URL', web.manager.booklore_client.base_url)
        mapping["audio_url"] = f"{_bl_audio_base}/book/{mapping['audio_source_id']}?tab=view"
    elif mapping["audio_source"] == "BookOrbit":
        mapping["abs_url"] = None
        _bo_audio_base = _public_link_base('BOOKORBIT_WEB_URL', web.os.environ.get("BOOKORBIT_SERVER") or "")
        mapping["audio_url"] = f"{_bo_audio_base}/book/{mapping['audio_source_id']}" if _bo_audio_base else None
    else:
        abs_display_base = _public_link_base('ABS_WEB_URL', web.manager.abs_client.base_url)
        mapping["abs_url"] = f"{abs_display_base}/item/{book.abs_id}"
        mapping["audio_url"] = mapping["abs_url"]

    mapping["booklore_id"] = web._get_cached_booklore_id(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if web.manager.booklore_client.is_configured() and mapping["booklore_id"]:
        booklore_display_base = _public_link_base('BOOKLORE_WEB_URL', web.manager.booklore_client.base_url)
        mapping["booklore_url"] = f"{booklore_display_base}/book/{mapping['booklore_id']}?tab=view"
    else:
        mapping["booklore_url"] = None

    # BookOrbit deep links — frontend book route is /book/:bookId.
    _bo_base = _public_link_base('BOOKORBIT_WEB_URL', web.os.environ.get("BOOKORBIT_SERVER") or "")
    if _bo_base and mapping.get("ebook_source") == "BookOrbit" and mapping.get("ebook_source_id"):
        mapping["bookorbit_url"] = f"{_bo_base}/book/{mapping['ebook_source_id']}"
    else:
        mapping["bookorbit_url"] = None
    if _bo_base and mapping.get("audio_source") == "BookOrbit" and mapping.get("audio_source_id"):
        mapping["bookorbit_audio_url"] = f"{_bo_base}/book/{mapping['audio_source_id']}"
    else:
        mapping["bookorbit_audio_url"] = None

    # Kavita stores BookBridge's source id at chapter level while its browser
    # route is series-based. Link to the configured web root rather than guess.
    _kavita_base = _public_link_base('KAVITA_WEB_URL', web.os.environ.get("KAVITA_SERVER") or "")
    mapping["kavita_url"] = (
        _kavita_base
        if _kavita_base and mapping.get("ebook_source") == "Kavita"
        else None
    )

    cached_goodreads = web._get_cached_goodreads_rating(book, cached_booklore_by_filename=cached_booklore_by_filename)
    for key in ("goodreads_rating", "goodreads_review_count"):
        if mapping[key] is None:
            mapping[key] = cached_goodreads.get(key)

    if mapping.get("hardcover_slug"):
        mapping["hardcover_url"] = f"https://hardcover.app/books/{mapping['hardcover_slug']}"
    elif mapping.get("hardcover_book_id"):
        mapping["hardcover_url"] = f"https://hardcover.app/books/{mapping['hardcover_book_id']}"
    else:
        mapping["hardcover_url"] = None

    if not mapping.get("storygraph_url") and mapping.get("storygraph_book_id"):
        mapping["storygraph_url"] = f"https://app.thestorygraph.com/books/{mapping['storygraph_book_id']}"

    mapping["sync_warning_pct"] = web._compute_dashboard_sync_warning_pct(mapping, integrations)
    mapping["is_out_of_sync"] = mapping["sync_warning_pct"] > 5.0
    mapping["unified_progress"] = min(max_progress, 100.0)
    mapping["last_sync"] = _format_dashboard_last_sync(latest_update_time)
    mapping["last_sync_unix"] = latest_update_time
    mapping["series_name"] = getattr(book, "series_name", None) or None
    mapping["series_sequence"] = getattr(book, "series_sequence", None)

    safe_cover = _browser_cover_url(
        mapping.get("audio_cover_url"),
        audio_source=mapping.get("audio_source"),
        audio_source_id=mapping.get("audio_source_id"),
        abs_id=book.abs_id,
    )
    # An ebook-only mapping has no audiobook to take a cover from, so fall back
    # to the library hosting the ebook. `audio_cover_url` stays audio-only.
    display_cover = safe_cover or _browser_cover_url(
        None,
        ebook_source=mapping.get("ebook_source"),
        ebook_source_id=mapping.get("ebook_source_id"),
    )
    if display_cover:
        mapping["cover_url"] = display_cover
    mapping["audio_cover_url"] = safe_cover

    reading_stats = reading_stats_by_book.get(book.abs_id)
    if reading_stats:
        mapping["reading_stats"] = reading_stats
        mapping["last_leader"] = reading_stats.get("last_leader")
        mapping["last_leader_service"] = _dashboard_leader_service(reading_stats.get("last_leader"))

    return mapping


def _build_dashboard_mappings(
    books,
    all_states,
    integrations,
    all_hardcover=None,
    all_storygraph=None,
    reading_stats_by_book=None,
    cached_booklore_by_filename=None,
    claim_times_by_book=None,
    bookfusion_by_book=None,
    bookorbit_authors=None,
    all_goodreads=None,
):
    web = _web_server()
    hardcover_by_book = {h.abs_id: h for h in (all_hardcover or [])}
    storygraph_by_book = {s.abs_id: s for s in (all_storygraph or [])}
    goodreads_by_book = {g.abs_id: g for g in (all_goodreads or [])}
    states_by_book = _group_dashboard_states_by_book(all_states)
    reading_stats_by_book = reading_stats_by_book or {}
    cached_booklore_by_filename = cached_booklore_by_filename or {}
    claim_times_by_book = claim_times_by_book or {}
    if bookfusion_by_book is None:
        bookfusion_by_book = _prefetch_bookfusion_links(books, integrations)
    if bookorbit_authors is None:
        bookorbit_authors = _prefetch_bookorbit_authors(books, integrations)

    ctc_aligned_book_ids = web.database_service.get_ctc_aligned_book_ids()
    readalong_alignment_book_ids = web.database_service.get_readalong_alignment_book_ids()
    readalong_ready_book_ids = web.database_service.get_readalong_ready_book_ids()
    sync_markers = web.database_service.get_sync_markers()
    mappings = []
    total_duration = 0
    total_listened = 0

    for book in books:
        mapping = _build_dashboard_mapping(
            book,
            states_by_book,
            integrations,
            hardcover_by_book,
            storygraph_by_book,
            reading_stats_by_book,
            cached_booklore_by_filename,
            claim_times_by_book,
            bookfusion_by_book=bookfusion_by_book,
            bookorbit_authors=bookorbit_authors,
            goodreads_by_book=goodreads_by_book,
        )
        mapping["ctc_aligned"] = book.abs_id in ctc_aligned_book_ids
        mapping["readalong_generated"] = book.abs_id in readalong_ready_book_ids
        mapping["readalong_ready"] = mapping["readalong_generated"] or web._has_linked_readalong(book)
        mapping["last_sync_leader"], mapping["last_sync_leader_at"] = sync_markers.get(book.abs_id, (None, 0))
        mapping["last_sync_leader_when"] = _format_dashboard_last_sync(mapping["last_sync_leader_at"])
        mapping["format_strip"] = _build_format_strip(mapping)

        # Read-along generation eligibility (Phase 6a). Cheap, locally-computable
        # conditions only -- whether BookOrbit can actually resolve the audio
        # entry's real track files needs a live API call (resolve_audiobook_folder)
        # and is not affordable per book on every dashboard render, so it is left
        # to the action's own handler; this only surfaces what is knowable from
        # already-loaded rows, mirroring the ctc_aligned badge above.
        audio_ok = (
            getattr(book, "audio_source", None) == "BookOrbit"
            and getattr(book, "sync_mode", "audiobook") != "ebook_only"
        )
        mapping["readalong_audio_ok"] = audio_ok
        if not audio_ok:
            mapping["readalong_eligible"] = False
            mapping["readalong_ineligible_reason"] = (
                "This mapping has no audiobook to generate a read-along from."
                if getattr(book, "sync_mode", "audiobook") == "ebook_only"
                else "Read-along generation requires a BookOrbit audio source."
            )
        elif book.abs_id not in readalong_alignment_book_ids:
            mapping["readalong_eligible"] = False
            mapping["readalong_ineligible_reason"] = "This book has no CTC or lexical alignment map yet."
        else:
            mapping["readalong_eligible"] = True
            mapping["readalong_ineligible_reason"] = None

        mappings.append(mapping)

        duration = mapping.get("duration", 0)
        progress_pct = mapping.get("unified_progress", 0)
        if duration > 0:
            total_duration += duration
            total_listened += (progress_pct / 100.0) * duration

    if total_duration > 0:
        overall_progress = round((total_listened / total_duration) * 100, 1)
    elif mappings:
        overall_progress = round(sum(m["unified_progress"] for m in mappings) / len(mappings), 1)
    else:
        overall_progress = 0

    return mappings, overall_progress
