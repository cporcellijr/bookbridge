"""C14 helpers extracted from web_server with no behavior change."""

from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _ebook_edition_label(book: dict) -> str:
    """Return a short edition label that distinguishes same-titled books.

    Priority:
    1. Non-empty subtitle -> return it stripped.
    2. Series name present:
       - If series index exists and series name (case/whitespace-insensitive)
         equals the book title, return "Book {index}".
       - If series index exists and series name differs from title, return
         "{series_name} #{index}".
       - If only series name, return it.
    3. Otherwise empty string.

    Index is coerced from int/float/str, dropping a trailing .0. Any unparseable
    index is treated as absent. All field access is defensive; never raises.
    """
    if not isinstance(book, dict):
        return ""

    subtitle = (book.get("subtitle") or "").strip()
    if subtitle:
        return subtitle

    series_name = (book.get("seriesName") or "").strip()
    if not series_name:
        return ""

    title = (book.get("title") or "").strip()

    # Coerce series index defensively
    raw_index = book.get("seriesIndex")
    index_str = None
    if raw_index is not None:
        try:
            if isinstance(raw_index, float):
                if raw_index.is_integer():
                    index_str = str(int(raw_index))
                else:
                    index_str = str(raw_index)
            elif isinstance(raw_index, int):
                index_str = str(raw_index)
            else:
                # str or other: try to parse as float then drop .0
                parsed = float(str(raw_index).strip())
                if parsed.is_integer():
                    index_str = str(int(parsed))
                else:
                    index_str = str(parsed)
        except (ValueError, TypeError):
            index_str = None

    if index_str:
        if series_name.lower() == title.lower():
            return f"Book {index_str}"
        return f"{series_name} #{index_str}"

    return series_name


def _record_suggestions_source_state(source_states, source, role, configured, supported=True, complete=None, count=None, error_type=None):
    source = "Grimmory" if source == "BookLore" else source
    state = source_states.setdefault(source, {"roles": {}})
    state["roles"][role] = {
        "configured": bool(configured),
        "supported": bool(supported),
        "complete": complete,
        "count": count,
        "error_type": error_type,
    }


def get_searchable_audiobooks(search_term, source_states=None, abs_catalog=None, full_refresh=False):
    """Get audiobook results from configured providers, optionally tracking scan health."""
    web = _web_server()
    discovery = source_states is not None
    adapters = {}
    try:
        clients = web._scan_user_clients()
        if clients.abs_client and clients.abs_client.is_configured():
            if discovery and abs_catalog is None:
                _record_suggestions_source_state(
                    source_states, "ABS", "audio", True, complete=False, error_type="CatalogUnavailable",
                )
            else:
                adapters["ABS"] = web.ABSAudioSourceAdapter(
                    clients.abs_client, web.container.data_dir(),
                    catalog_items=abs_catalog if discovery else None,
                )
        if clients.booklore_client and clients.booklore_client.is_configured():
            catalog = clients.booklore_client.get_all_books(include_all_ids=True, force_refresh=full_refresh) if discovery else None
            adapters["BookLore"] = web.BookLoreAudioSourceAdapter(
                clients.booklore_client, web.container.data_dir(), catalog_items=catalog,
            )
        _bo_client = getattr(clients, "bookorbit_client", None)
        if _bo_client and _bo_client.is_configured():
            if full_refresh:
                _bo_client.get_all_books(force_refresh=True)
            adapters["BookOrbit"] = web.BookOrbitAudioSourceAdapter(_bo_client, web.container.data_dir())
    except Exception as e:
        if discovery:
            _record_suggestions_source_state(
                source_states, "_client_registry", "audio", True, complete=False, error_type=type(e).__name__,
            )
            web.logger.warning("Could not build user-scoped audio adapters (%s)", type(e).__name__, exc_info=True)
            return []
        if not web._acting_user_may_use_global_config():
            web.logger.warning(
                "Could not build user-scoped audio adapters; global adapters withheld (%s)",
                type(e).__name__, exc_info=True,
            )
            return []
        web.logger.debug("Could not build user-scoped audio adapters, falling back to globals: %s", e)
        adapters = web.container.audio_source_adapters() if hasattr(web.container, "audio_source_adapters") else {}
    results = []
    seen = set()
    per_adapter_counts = {}

    for source_name, adapter in adapters.items():
        try:
            provider_results = adapter.search(search_term)
        except Exception as e:
            web.logger.warning(
                "⚠️ Audiobook search failed for %s (%s)", source_name, type(e).__name__,
                exc_info=not discovery,
            )
            per_adapter_counts[source_name] = f"error:{type(e).__name__}"
            if discovery:
                _record_suggestions_source_state(
                    source_states, source_name, "audio", True, complete=False, error_type=type(e).__name__,
                )
            continue
        per_adapter_counts[source_name] = len(provider_results) if provider_results else 0
        if discovery:
            if source_name == "ABS":
                complete = abs_catalog is not None and not getattr(adapter, "catalog_incomplete", False)
            elif source_name == "BookLore":
                complete = bool(getattr(clients.booklore_client, "catalog_complete", False))
            else:
                complete = bool(getattr(clients.bookorbit_client, "catalog_complete", False))
            _record_suggestions_source_state(
                source_states, source_name, "audio", True, complete=complete,
                count=len(provider_results or []),
                error_type=None if complete else "CatalogIncomplete",
            )

        for result in provider_results or []:
            if not isinstance(result, web.AudioResult):
                continue
            key = (result.source, result.source_id)
            if key in seen:
                continue
            seen.add(key)
            results.append(result)

    results.sort(key=lambda item: (item.title or item.display_name or "").lower())
    web.logger.debug(
        "get_searchable_audiobooks(query=%r): adapters=%s, deduped_total=%d",
        search_term, per_adapter_counts, len(results),
    )
    if discovery:
        for source_name, client in (
            ("BookLore", clients.booklore_client),
            ("BookOrbit", getattr(clients, "bookorbit_client", None)),
        ):
            state = source_states.get("Grimmory" if source_name == "BookLore" else source_name, {}).get("roles", {}).get("audio", {})
            if not state.get("configured") or state.get("complete") is not True:
                continue
            try:
                if source_name == "BookLore":
                    books = client.get_all_books(include_all_ids=True)
                    state["unknown_companion_count"] = sum(
                        bool(book.get("_needs_detail") and not client._book_supports_audiobook(book)
                             and not client._has_audio_shape_fields(book))
                        for book in books
                    )
                    missing_title = any(
                        (client._book_supports_audiobook(book) or (
                            book.get("_needs_detail") and not client._has_audio_shape_fields(book)
                        )) and not (book.get("title") or book.get("fileName"))
                        for book in books
                    )
                else:
                    books = client.get_all_books()
                    missing_title = any(
                        client._info_offers_kind(book, "audiobook") and not book.get("title")
                        for book in books
                    )
                if missing_title:
                    _record_suggestions_source_state(
                        source_states, source_name, "audio", True, complete=False,
                        error_type="MissingAudioTitle",
                    )
            except Exception as e:
                _record_suggestions_source_state(
                    source_states, source_name, "audio", True, complete=False, error_type=type(e).__name__,
                )
    return results


def _punctuation_search_key(value):
    """Compare title text without punctuation or whitespace."""
    return ''.join(char.lower() for char in (value or '') if char.isalnum())


def _punctuation_search_fragment(term):
    """Return a short phrase shared by punctuated and plain long titles."""
    web = _web_server()
    if ' - ' in (term or ''):
        return ''
    words = web.re.findall(r'[^\W\d_]+', term or '')
    if words and words[0].lower() in ('the', 'a', 'an'):
        words = words[1:]
    return ' '.join(words[:2]) if len(words) >= 4 else ''


def _audiobook_search_variants(term):
    """Progressive query relaxations for a (possibly filename-derived) term.

    Yields the raw term, then removes filename suffixes and punctuation.
    Long titles also get a short phrase so strict provider searches can find
    editions that differ by punctuation within the title.
    """
    web = _web_server()
    term = (term or "").strip()
    variants = []

    def _add(value):
        value = (value or "").strip()
        if value and value not in variants:
            variants.append(value)

    def _add_hyphen_space_variants(value):
        value = (value or "").strip()
        if not value:
            return

        if "-" in value:
            spaced = web.re.sub(r"\s*-\s*", " ", value).strip()
            _add(spaced)
            return

        words = web.re.split(r"\s+", value)
        if 2 <= len(words) <= 3:
            for index in range(len(words) - 1):
                hyphenated_words = words[:]
                hyphenated_words[index:index + 2] = [f"{words[index]}-{words[index + 1]}"]
                _add(" ".join(hyphenated_words))

    _add(term)
    no_ext = web.re.sub(r'\.(epub|pdf|mobi|azw3?|cbz|cbr|m4b|mp3)$', '', term, flags=web.re.IGNORECASE)
    no_year = web.re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', no_ext).strip()
    no_edition = web.re.sub(
        r'\s*\((?:unabridged|abridged|audio(?:book)?|e-?book|kindle|retail|edition)\)\s*$',
        '',
        no_year,
        flags=web.re.IGNORECASE,
    ).strip()
    _add(no_year)
    _add(no_edition)
    if ' - ' in no_edition:
        title_part = no_edition.split(' - ')[0]
        _add(title_part)
        _add_hyphen_space_variants(title_part)
    else:
        _add_hyphen_space_variants(no_edition)
    search_title = no_edition.split(' - ')[0]
    _add(' '.join(web.re.findall(r'\w+', search_title)))
    if ' - ' not in no_edition:
        _add(_punctuation_search_fragment(search_title))
    return variants


def _search_audiobooks_with_fallback(term):
    """Merge audiobook hits across title spellings and source catalogs."""
    web = _web_server()
    results = []
    seen = set()
    fragment = _punctuation_search_fragment(term)
    query_key = _punctuation_search_key(term)
    for index, variant in enumerate(_audiobook_search_variants(term)):
        for result in get_searchable_audiobooks(variant):
            if (fragment and variant == fragment and variant != term
                    and query_key not in _punctuation_search_key(result.title)):
                continue
            key = (result.source, result.source_id)
            if key not in seen:
                seen.add(key)
                results.append(result)
                if index > 0:
                    web.logger.debug("Audiobook search matched on relaxed term %r (from %r)", variant, term)
    results.sort(key=lambda item: (item.title or item.display_name or '').lower())
    return results


def get_suggestion_audiobooks(source_states=None, abs_catalog=None, full_refresh=False):
    """Return provider-normalized audiobook records for suggestions scan."""
    web = _web_server()
    records = []
    for item in get_searchable_audiobooks(
        "", source_states=source_states, abs_catalog=abs_catalog, full_refresh=full_refresh,
    ):
        if not isinstance(item, web.AudioResult):
            continue

        audio_source = (item.source or "").strip() or "ABS"
        source_id = str(item.source_id or "").strip()
        if not source_id:
            continue
        bridge_key = web._build_bridge_key(audio_source, source_id)
        if not bridge_key:
            continue

        title = (item.title or item.display_name or bridge_key).strip()
        author = (item.authors or "").strip()
        cover_url = web._browser_cover_url(
            item.cover_url,
            audio_source=audio_source,
            audio_source_id=source_id,
            abs_id=bridge_key,
        )
        records.append(
            {
                "bridge_key": bridge_key,
                "audio_source": audio_source,
                "audio_source_id": source_id,
                "audio_title": title,
                "audio_subtitle": (item.subtitle or "").strip(),
                "audio_series": (item.series_label or "").strip(),
                "audio_series_name": item.series_name,
                "audio_series_sequence": item.series_sequence,
                "audio_series_source": item.series_source,
                "audio_author": author,
                "audio_language": item.language or "",
                "audio_duration": item.duration,
                "audio_cover_url": cover_url,
                "audio_path": item.path or "",
                "audio_provider_book_id": str(item.provider_book_id or source_id),
                "audio_provider_file_id": str(item.provider_file_id or ""),
                # Legacy aliases maintained for compatibility with existing templates/session keys.
                "id": bridge_key,
                "title": title,
                "authors": author,
                "duration": item.duration,
                "cover_url": cover_url,
            }
        )

    return records


class EbookResult:
    """Provider or local ebook metadata shared by search and suggestions scans."""

    def __init__(
        self, name, title=None, subtitle=None, authors=None, booklore_id=None,
        path=None, source=None, source_id=None, abs_identifier=None, language=None,
        series_name=None, series_sequence=None, source_file_id=None,
        ebook_file_id=None, file_mtime_ns=None, file_size=None,
    ):
        web = _web_server()
        self.name = name
        self.title = title or web.Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.language = str(language or '').strip()
        self.booklore_id = booklore_id
        self.path = path
        self.source = source
        self.source_id = source_id if source_id is not None else booklore_id
        self.abs_identifier = abs_identifier
        self.series_name = series_name
        self.series_sequence = series_sequence
        self.source_file_id = source_file_id
        self.ebook_file_id = ebook_file_id
        self.file_mtime_ns = file_mtime_ns
        self.file_size = file_size
        self.has_metadata = booklore_id is not None or (title is not None and title != name)

    @property
    def display_name(self):
        """Format: 'Title: Subtitle - Author' for sources with metadata, title for filesystem."""
        if self.has_metadata and self.title:
            full_title = self.title
            if self.subtitle:
                full_title = f"{self.title}: {self.subtitle}"
            if self.authors:
                return f"{full_title} - {self.authors}"
            return full_title
        return self.title

    @property
    def stem(self):
        web = _web_server()
        return web.Path(self.name).stem

    def __str__(self):
        return self.name
