"""C15 helpers extracted from web_server with no behavior change."""

from importlib import import_module
from typing import Optional

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _ebook_is_provider(ebook):
    """True for a library-backed ebook (BookOrbit/Grimmory/ABS/CWA), not a bare file."""
    return bool(getattr(ebook, "source", None) and getattr(ebook, "source", None) != "Local File")


def _search_ebooks_with_fallback(term):
    """Search ebooks across the raw + relaxed terms and merge.

    The library providers (BookOrbit/Grimmory) use strict title search, so a
    filename-stem term ("Title - Author (2026)") only matches the local file. The
    relaxed title lets the provider match; results are deduped by filename with the
    provider entry preferred over the bare local file so the picker offers the
    library copy (whose progress actually syncs).
    """
    web = _web_server()
    by_name = {}
    order = []
    fragment = web._punctuation_search_fragment(term)
    query_key = web._punctuation_search_key(term)
    for variant in web._audiobook_search_variants(term):
        for ebook in get_searchable_ebooks(variant):
            if (fragment and variant == fragment and variant != term
                    and query_key not in web._punctuation_search_key(
                        getattr(ebook, 'title', None) or ebook.name)):
                continue
            key = (getattr(ebook, "name", "") or "").lower()
            if not key:
                continue
            existing = by_name.get(key)
            if existing is None:
                by_name[key] = ebook
                order.append(key)
            elif _ebook_is_provider(ebook) and not _ebook_is_provider(existing):
                by_name[key] = ebook  # upgrade a local-file hit to the library copy
    return [by_name[key] for key in order]


def _first_not_none(*values):
    return next((value for value in values if value is not None), None)


def _ebook_title_key(value):
    """Normalize a title/filename-stem to an alphanumeric key for joining."""
    web = _web_server()
    return web.re.sub(r'[\W_]+', '', (value or '').lower())


def _scan_excludes_local_files() -> bool:
    """Scans omit the server's local folder when the admin setting is on or a regular user is acting."""
    web = _web_server()
    return web.suggestions_exclude_local_files(web.database_service)


def _submitted_ebook_path(raw_path) -> Optional[str]:
    """A request's ebook path, dropped when it is a regular user's server path.

    Their selections resolve through their own provider, so a submitted server path
    (which a forged provider label could point at the local folder) is never trusted.
    A provider download URL is kept.
    """
    web = _web_server()
    path = str(raw_path or "").strip() or None
    if path and "://" not in path and web.acting_user_is_regular(web.database_service):
        return None
    return path


def _refuse_regular_user_local_selection(
    ebook_source, ebook_source_path=None, ebook_source_id=None, ebook_filename=None,
) -> bool:
    """True (and logged) when a regular user submits a server-local ebook selection."""
    web = _web_server()
    if not web.acting_user_is_regular(web.database_service):
        return False
    source = web._normalize_text_source_type(ebook_source)
    path = str(ebook_source_path or "").strip()
    # A provider entry (BookOrbit, Grimmory...) carries its own server path; only a
    # selection with no provider behind it is the server's local folder.
    local = source == "Local File" or (
        not source and not ebook_source_id and (bool(ebook_filename) or (bool(path) and "://" not in path))
    )
    if local:
        web.logger.info(
            "🔒 Refused a server-local ebook selection from a non-admin user (%s)",
            web.sanitize_log_data(ebook_filename or path or "no filename"),
        )
    return local


def _build_local_ebook_title_index(scan_metadata=None):
    """Map normalized title -> filename for every epub on the local /books disk.

    BookBridge and BookOrbit share the same files, so this lets us pair
    BookOrbit's clean metadata (title/author, no filename) with real filenames
    without a per-book BookOrbit detail call. Indexes the full stem, the 'Title'
    portion before ' - ' ('Title - Author (year).epub'), and the title in the
    file's own metadata, which BookOrbit's title comes from whatever the naming
    scheme ('Author - Series 15 - Title.epub', #549)."""
    web = _web_server()
    index = {}
    try:
        if web.EBOOK_DIR.exists():
            for eb in web.EBOOK_DIR.rglob('*'):
                if eb.suffix.lower() not in web.LINKABLE_EBOOK_EXTENSIONS or not eb.is_file():
                    continue
                if web.is_generated_readalong_epub(eb.name) or web.is_in_hidden_folder(eb, web.EBOOK_DIR):
                    continue
                stem = eb.stem
                title_part = stem.split(" - ", 1)[0]
                metadata, series = web.read_local_ebook_metadata(eb), web.read_local_ebook_series(eb)
                metadata_title, metadata_authors = metadata
                if scan_metadata is not None:
                    stat = eb.stat()
                    scan_metadata[str(eb)] = {
                        "path": eb,
                        "metadata": metadata,
                        "series": series,
                        "mtime_ns": stat.st_mtime_ns,
                        "size": stat.st_size,
                    }
                for key in (_ebook_title_key(title_part), _ebook_title_key(stem), _ebook_title_key(metadata_title)):
                    if key:
                        index.setdefault(key, eb.name)
    except Exception as e:
        if scan_metadata is not None:
            scan_metadata["_error_type"] = type(e).__name__
        web.logger.warning(f"⚠️ Failed to build local ebook title index: {e}", exc_info=True)
    return index


def get_searchable_ebooks(
    search_term, inventory_candidates=None, source_states=None,
    full_refresh=False, local_metadata=None, abs_catalog=None,
):
    """Get ebook candidates, optionally collecting undeduped scan inventory."""
    web = _web_server()
    discovery = source_states is not None
    regular_user = web.acting_user_is_regular(web.database_service)
    exclude_local_files = discovery and _scan_excludes_local_files()
    skip_local = exclude_local_files or regular_user
    inventory_candidates = inventory_candidates if inventory_candidates is not None else []
    scan_local_metadata = local_metadata if local_metadata is not None else ({} if discovery else None)
    results = []
    found_filenames = set()
    found_stems = set()  # To dedupe by title stem
    bookorbit_filenames = set()
    try:
        clients = web._scan_user_clients()
    except Exception as exc:
        if discovery:
            web._record_suggestions_source_state(
                source_states, "_client_registry", "ebook", True,
                complete=False, error_type=type(exc).__name__,
            )
            web.logger.warning("Could not load user-scoped ebook clients (%s)", type(exc).__name__, exc_info=True)
            return results
        raise

    local_index = None
    if discovery:
        if web.EBOOK_DIR.exists() and not regular_user:
            local_index = _build_local_ebook_title_index(scan_local_metadata)
            error_type = scan_local_metadata.get("_error_type") if isinstance(scan_local_metadata, dict) else None
            web._record_suggestions_source_state(
                source_states, "Local File", "ebook", not exclude_local_files,
                complete=None if exclude_local_files else error_type is None,
                count=None if exclude_local_files or error_type else len(scan_local_metadata),
                error_type=None if exclude_local_files else error_type,
            )
        else:
            web._record_suggestions_source_state(
                source_states, "Local File", "ebook", False, complete=None,
            )

    # 1. Grimmory
    booklore_client = getattr(clients, "booklore_client", None)
    booklore_configured = bool(booklore_client and booklore_client.is_configured())
    if discovery:
        web._record_suggestions_source_state(
            source_states, "Grimmory", "ebook", booklore_configured,
            complete=None if not booklore_configured else False,
        )
    if booklore_configured:
        try:
            if search_term:
                books = booklore_client.search_books(search_term)
            else:
                # The ID view supplies every provider book to inverse discovery; the
                # default filename view below remains the legacy pair-candidate pool.
                if discovery:
                    all_id_books = booklore_client.get_all_books(
                        include_all_ids=True, force_refresh=full_refresh and "audio" not in source_states.get("Grimmory", {}).get("roles", {}),
                    )
                    web._record_suggestions_source_state(
                        source_states, "Grimmory", "ebook", True,
                        complete=bool(getattr(booklore_client, "catalog_complete", False)),
                        count=len(all_id_books),
                        error_type=None if booklore_client.catalog_complete else "CatalogIncomplete",
                    )
                    for book in all_id_books or []:
                        primary = book.get('primaryFile') or {}
                        fname = book.get('fileName') or primary.get('fileName') or ''
                        file_type = str(book.get('bookType') or primary.get('bookType') or '').upper()
                        if not web.is_linkable_ebook_filename(fname) and file_type != 'EPUB':
                            continue
                        series_name, series_sequence = web._series_from_library_detail(book)
                        inventory_candidates.append(web.EbookResult(
                            name=fname,
                            title=book.get('title'),
                            authors=book.get('authors'),
                            language=book.get('language'),
                            booklore_id=book.get('id'),
                            source='Grimmory',
                            path=book.get('filePath') or primary.get('filePath') or book.get('path'),
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=_first_not_none(book.get('sourceFileId'), primary.get('id')),
                            ebook_file_id=book.get('ebookFileId'),
                        ))
                    books = booklore_client.get_all_books()
                else:
                    books = booklore_client.get_all_books()
            if books:
                if search_term:
                    from src.utils.provider_ebook_files import ebook_files
                    expanded = []
                    for book in books:
                        files = ebook_files(book, "Booklore")
                        # Older Grimmory servers expose only a legacy primary filename.
                        if not files:
                            expanded.append(book)
                        for file in files:
                            expanded.append(dict(book, fileName=file["filename"], sourceFileId=file["id"]))
                    books = expanded
                for b in books:
                    fname = b.get('fileName', '')
                    if web.is_linkable_ebook_filename(fname):
                        found_filenames.add(fname.lower())
                        found_stems.add(web.Path(fname).stem.lower())
                        series_name, series_sequence = web._series_from_library_detail(b)
                        results.append(web.EbookResult(
                            name=fname,
                            title=b.get('title'),
                            subtitle=web._ebook_edition_label(b),
                            authors=b.get('authors'),
                            language=b.get('language'),
                            booklore_id=b.get('id'),
                            path=b.get('filePath') or b.get('filepath') or b.get('path'),
                            source='Grimmory',
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=b.get('sourceFileId'),
                            ebook_file_id=b.get('ebookFileId'),
                        ))
        except Exception as e:
            if discovery:
                web._record_suggestions_source_state(
                    source_states, "Grimmory", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                web.logger.warning("⚠️ Grimmory scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                web.logger.warning(f"⚠️ Grimmory search failed: {e}", exc_info=True)

    # 1b. BookOrbit
    bookorbit_client = getattr(clients, "bookorbit_client", None)
    bookorbit_configured = bool(bookorbit_client and bookorbit_client.is_configured())
    if discovery:
        web._record_suggestions_source_state(
            source_states, "BookOrbit", "ebook", bookorbit_configured,
            complete=None if not bookorbit_configured else False,
        )
    if bookorbit_configured:
        try:
            if search_term:
                bo_books = bookorbit_client.search_ebooks(search_term, include_files=True)
                local_index = None
            else:
                if discovery and full_refresh and "audio" not in source_states.get("BookOrbit", {}).get("roles", {}):
                    bookorbit_client.get_all_books(force_refresh=True)
                bo_books = bookorbit_client.get_all_ebooks()
                if not discovery and not regular_user:
                    local_index = _build_local_ebook_title_index()
                if discovery:
                    complete = bool(getattr(bookorbit_client, "catalog_complete", False))
                    web._record_suggestions_source_state(
                        source_states, "BookOrbit", "ebook", True,
                        complete=complete, count=len(bo_books or []),
                        error_type=None if complete else "CatalogIncomplete",
                    )
                    for book in bo_books or []:
                        ebook_format = str(book.get("ebookFormat") or "").lower().lstrip(".")
                        fname = book.get("fileName") or ""
                        if not web.is_linkable_ebook_filename(fname) and ebook_format not in {"epub", "cbz"}:
                            continue
                        series_name, series_sequence = web._series_from_library_detail(book)
                        inventory_candidates.append(web.EbookResult(
                            name=fname,
                            title=book.get("title"),
                            authors=book.get("authors"),
                            language=book.get("language"),
                            source="BookOrbit",
                            source_id=book.get("id"),
                            path=book.get("filePath") or book.get("filepath") or book.get("path"),
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=book.get("sourceFileId"),
                            ebook_file_id=book.get("ebookFileId"),
                        ))
            for b in bo_books or []:
                # The list response already contains all file names. Keep its
                # non-primary editions behind the provider card, not as local
                # fallback choices. Do not suppress other editions by title/stem.
                bookorbit_filenames.update(
                    name.lower() for name in (b.get('ebookFilenames') or [])
                    if isinstance(name, str) and web.is_linkable_ebook_filename(name)
                )
                fname = b.get('fileName') or ''
                if not fname and local_index is not None:
                    fname = local_index.get(_ebook_title_key(b.get('title'))) or ''
                if not fname and (discovery or local_index is not None):
                    # Index miss, or no index for a regular user: ask BookOrbit for this one
                    # file (#586). Interactive picker calls skip it; detail is cached 1h.
                    fname = (bookorbit_client._enrich_ebook(b.get('id'), b) or {}).get('fileName') or ''
                if not web.is_linkable_ebook_filename(fname):
                    continue
                if fname.lower() in found_filenames:
                    continue
                found_filenames.add(fname.lower())
                found_stems.add(web.Path(fname).stem.lower())
                series_name, series_sequence = web._series_from_library_detail(b)
                results.append(web.EbookResult(
                    name=fname,
                    title=b.get('title'),
                    authors=b.get('authors'),
                    language=b.get('language'),
                    path=b.get('filePath') or b.get('filepath') or b.get('path'),
                    source='BookOrbit',
                    source_id=b.get('id'),
                    subtitle=web._ebook_edition_label(b),
                    series_name=series_name,
                    series_sequence=series_sequence,
                    source_file_id=b.get('sourceFileId'),
                    ebook_file_id=b.get('ebookFileId'),
                ))
        except Exception as e:
            if discovery:
                web._record_suggestions_source_state(
                    source_states, "BookOrbit", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                web.logger.warning("⚠️ BookOrbit scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                web.logger.warning(f"⚠️ BookOrbit search failed: {e}", exc_info=True)

    # BookFusion is searchable but does not expose a supported full catalog.
    bookfusion_client = getattr(clients, "bookfusion_client", None)
    if discovery:
        web._record_suggestions_source_state(
            source_states, "BookFusion", "ebook",
            bool(bookfusion_client and bookfusion_client.is_configured()),
            supported=False, complete=None,
        )

    # 1c. BookFusion existing user library links. Search-only because BookFusion
    # does not provide a bridge-side EPUB download in Phase 1/3.
    if search_term and clients.bookfusion_client.is_configured():
        try:
            bf_books = clients.bookfusion_client.search_books(page=1, per_page=50, query=search_term)
            query_lower = search_term.lower()
            for b in bf_books or []:
                bf_id = b.get("id") or b.get("book_id")
                if bf_id in (None, ""):
                    continue
                title = str(b.get("title") or b.get("name") or f"BookFusion {bf_id}").strip()
                authors = web._coerce_author_display(b.get("authors") or b.get("author"))
                haystack = f"{title} {authors}".lower()
                if query_lower and query_lower not in haystack:
                    continue
                fname = f"bookfusion_{bf_id}.epub"
                results.append(web.EbookResult(
                    name=fname,
                    title=title,
                    authors=authors,
                    language=b.get('language'),
                    path=None,
                    source='BookFusion',
                    source_id=bf_id,
                ))
        except Exception as e:
            web.logger.warning(f"⚠️ BookFusion search failed: {e}", exc_info=True)

    # 1d. Kavita
    kavita_client = getattr(clients, "kavita_client", None)
    kavita_configured = bool(kavita_client and kavita_client.is_configured())
    if discovery:
        web._record_suggestions_source_state(
            source_states, "Kavita", "ebook", kavita_configured,
            complete=None if not kavita_configured else False,
        )
    if kavita_configured:
        try:
            if search_term:
                kavita_books = kavita_client.search_ebooks(search_term)
            elif discovery:
                kavita_books = kavita_client.get_all_books(
                    force_refresh=full_refresh, require_complete=True,
                )
                complete = bool(getattr(kavita_client, "catalog_complete", False))
                web._record_suggestions_source_state(
                    source_states, "Kavita", "ebook", True,
                    complete=complete, count=len(kavita_books or []),
                    error_type=None if complete else "CatalogIncomplete",
                )
            else:
                kavita_books = kavita_client.get_all_books()
            for book in kavita_books or []:
                filename = book.get('fileName') or book.get('filename') or ''
                if discovery and not search_term:
                    if web.is_linkable_ebook_filename(filename):
                        series_name, series_sequence = web._series_from_library_detail(book)
                        inventory_candidates.append(web.EbookResult(
                            name=filename,
                            title=book.get('title'),
                            authors=book.get('authors') or book.get('author'),
                            language=book.get('language'),
                            source='Kavita',
                            source_id=book.get('id'),
                            path=book.get('filePath') or book.get('path'),
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=book.get('sourceFileId'),
                            ebook_file_id=book.get('ebookFileId'),
                        ))
                if not web.is_linkable_ebook_filename(filename):
                    continue
                if filename.lower() in found_filenames:
                    continue
                found_filenames.add(filename.lower())
                found_stems.add(web.Path(filename).stem.lower())
                series_name, series_sequence = web._series_from_library_detail(book)
                results.append(web.EbookResult(
                    name=filename,
                    title=book.get('title'),
                    subtitle=web._ebook_edition_label(book),
                    authors=book.get('authors') or book.get('author'),
                    language=book.get('language'),
                    path=book.get('filePath') or book.get('path'),
                    source='Kavita',
                    source_id=book.get('id'),
                    series_name=series_name,
                    series_sequence=series_sequence,
                    source_file_id=book.get('sourceFileId'),
                    ebook_file_id=book.get('ebookFileId'),
                ))
        except Exception as e:
            if discovery:
                web._record_suggestions_source_state(
                    source_states, "Kavita", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                web.logger.warning("Kavita scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                web.logger.warning("Kavita search failed: %s", e, exc_info=True)

    # 2. ABS ebook libraries
    abs_client = getattr(clients, "abs_client", None)
    abs_configured = bool(abs_client and abs_client.is_configured())
    if discovery:
        web._record_suggestions_source_state(
            source_states, "ABS", "ebook", abs_configured,
            complete=None if not abs_configured else False,
        )
    if search_term:
        try:
            if abs_client:
                abs_ebooks = abs_client.search_ebooks(search_term)
                if abs_ebooks:
                    for ab in abs_ebooks:
                        ebook_files = abs_client.get_ebook_files(ab['id'])
                        if ebook_files:
                            ef = ebook_files[0]
                            fname = f"{ab['id']}_abs.{ef['ext']}"
                            if fname.lower() not in found_filenames:
                                results.append(web.EbookResult(
                                    name=fname,
                                    title=ab.get('title'),
                                    authors=ab.get('author'),
                                    language=ab.get('language'),
                                    source='ABS',
                                    source_id=ab.get('id'),
                                    subtitle=web._ebook_edition_label(ab)
                                ))
                                found_filenames.add(fname.lower())
                                if ab.get('title'):
                                    found_stems.add(ab['title'].lower().strip())
        except Exception as e:
            if discovery:
                web._record_suggestions_source_state(
                    source_states, "ABS", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                web.logger.warning("⚠️ ABS ebook search failed (%s)", type(e).__name__, exc_info=True)
            else:
                web.logger.warning(f"⚠️ ABS ebook search failed: {e}", exc_info=True)

    if discovery and abs_configured:
        try:
            if abs_catalog is None:
                abs_catalog = abs_client.get_library_catalog(force_refresh=full_refresh)
            complete = isinstance(abs_catalog, list)
            web._record_suggestions_source_state(
                source_states, "ABS", "ebook", True, complete=complete,
                count=len(abs_catalog) if complete else None,
                error_type=None if complete else "CatalogUnavailable",
            )
            for item in abs_catalog or []:
                media = item.get("media") or {}
                file_info = media.get("ebookFile")
                file_data = file_info if isinstance(file_info, dict) else {}
                file_text = file_info if isinstance(file_info, str) else ""
                ext = str(
                    file_data.get("ext") or media.get("ebookFormat")
                    or web.Path(file_text).suffix.lstrip(".")
                ).lower().lstrip(".")
                if not web.is_linkable_ebook_filename("file." + ext):
                    continue
                source_id = item.get("id")
                if source_id is None or str(source_id).strip() == "":
                    continue
                metadata = media.get("metadata") or item.get("metadata") or {}
                fname = file_data.get("fileName") or file_data.get("filename") or f"{source_id}_abs.{ext}"
                series_name, series_sequence = web._series_from_abs_metadata(metadata)
                inventory_candidates.append(web.EbookResult(
                    name=fname,
                    title=metadata.get("title") or item.get("name"),
                    authors=metadata.get("authorName") or "",
                    language=metadata.get("language") or "",
                    source="ABS",
                    source_id=source_id,
                    series_name=series_name,
                    series_sequence=series_sequence,
                    source_file_id=(
                        _first_not_none(file_data.get("ino"), file_data.get("id"), file_data.get("fileId"))
                    ),
                    ebook_file_id=(
                        _first_not_none(file_data.get("ino"), file_data.get("id"), file_data.get("fileId"))
                    ),
                    path=file_data.get("path") or file_data.get("filePath"),
                ))
        except Exception as e:
            web._record_suggestions_source_state(
                source_states, "ABS", "ebook", True,
                complete=False, error_type=type(e).__name__,
            )
            web.logger.warning("⚠️ ABS catalog capture failed (%s)", type(e).__name__, exc_info=True)

    # 3. CWA (Calibre-Web Automated)
    library_service = getattr(clients, "library_service", None)
    cwa_client = getattr(library_service, "cwa_client", None) if library_service else None
    cwa_configured = bool(cwa_client and cwa_client.is_configured())
    if discovery:
        web._record_suggestions_source_state(
            source_states, "CWA", "ebook", cwa_configured,
            complete=None if not cwa_configured else False,
        )
    if search_term:
        try:
            if cwa_configured:
                cwa_results = cwa_client.search_ebooks(search_term)
                for cr in cwa_results or []:
                    fname = f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}"
                    if fname.lower() in found_filenames:
                        continue
                    cwa_id = cr.get('id')
                    abs_identifier = None
                    try:
                        calibre_resolver = web.container.calibre_identifier_resolver()
                    except Exception:
                        calibre_resolver = None
                    resolver_enabled = bool(calibre_resolver and calibre_resolver.is_enabled())
                    if resolver_enabled and cwa_id is not None:
                        try:
                            abs_identifier = calibre_resolver.get_abs_id(cwa_id)
                        except Exception as e:
                            web.logger.debug("Calibre identifier lookup failed for %s (%s)", cwa_id, type(e).__name__)
                    results.append(web.EbookResult(
                        name=fname,
                        title=cr.get('title'),
                        authors=cr.get('author'),
                        language=cr.get('language'),
                        path=cr.get('download_url'),
                        source='CWA',
                        source_id=cwa_id,
                        abs_identifier=abs_identifier,
                    ))
                    found_filenames.add(fname.lower())
                    if cr.get('title'):
                        found_stems.add(cr['title'].lower().strip())
        except Exception as e:
            if discovery:
                web._record_suggestions_source_state(
                    source_states, "CWA", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                web.logger.warning("⚠️ CWA search failed (%s)", type(e).__name__, exc_info=True)
            else:
                web.logger.warning(f"⚠️ CWA search failed: {e}", exc_info=True)

    if discovery and cwa_configured:
        try:
            cwa_results = cwa_client.get_all_ebooks(force_refresh=full_refresh)
            complete = bool(getattr(cwa_client, "catalog_complete", False))
            web._record_suggestions_source_state(
                source_states, "CWA", "ebook", True, complete=complete,
                count=len(cwa_results or []),
                error_type=None if complete else "CatalogIncomplete",
            )
            try:
                calibre_resolver = web.container.calibre_identifier_resolver()
            except Exception:
                calibre_resolver = None
            resolver_enabled = bool(calibre_resolver and calibre_resolver.is_enabled())
            abs_ids = None
            if resolver_enabled:
                try:
                    abs_ids = calibre_resolver.get_abs_ids([
                        record.get("id") for record in cwa_results or []
                        if record.get("id") is not None
                    ])
                    if abs_ids is None:
                        web._record_suggestions_source_state(
                            source_states, "CWA", "ebook", True,
                            complete=False, error_type="CalibreIdentifierUnavailable",
                        )
                except Exception as e:
                    web._record_suggestions_source_state(
                        source_states, "CWA", "ebook", True,
                        complete=False, error_type=type(e).__name__,
                    )
                    abs_ids = None
            for record in cwa_results or []:
                cwa_id = record.get("id")
                if cwa_id is None or str(cwa_id).strip() == "":
                    continue
                ext = str(record.get("ext") or "epub").lower().lstrip(".")
                fname = f"cwa_{cwa_id}.{ext}"
                if not web.is_linkable_ebook_filename(fname):
                    continue
                series_name, series_sequence = web._series_from_library_detail(record)
                inventory_candidates.append(web.EbookResult(
                    name=fname,
                    title=record.get("title"),
                    authors=record.get("author") or "",
                    language=record.get("language") or "",
                    source="CWA",
                    source_id=cwa_id,
                    abs_identifier=(abs_ids or {}).get(str(cwa_id)) if resolver_enabled else None,
                    series_name=series_name,
                    series_sequence=series_sequence,
                ))
        except Exception as e:
            web._record_suggestions_source_state(
                source_states, "CWA", "ebook", True,
                complete=False, error_type=type(e).__name__,
            )
            web.logger.warning("⚠️ CWA catalog capture failed (%s)", type(e).__name__, exc_info=True)

    # 4. Search filesystem (Local) - LOW PRIORITY
    if web.EBOOK_DIR.exists() and not skip_local:
        try:
            if discovery and scan_local_metadata is not None:
                local_files = [
                    details for key, details in scan_local_metadata.items()
                    if not str(key).startswith("_")
                ]
            else:
                local_files = [
                    {"path": eb, "metadata": None, "series": None,
                     "mtime_ns": eb.stat().st_mtime_ns, "size": eb.stat().st_size}
                    for eb in web.EBOOK_DIR.rglob('*')
                    if eb.suffix.lower() in web.LINKABLE_EBOOK_EXTENSIONS and eb.is_file()
                    and not web.is_generated_readalong_epub(eb.name)
                    and not web.is_in_hidden_folder(eb, web.EBOOK_DIR)
                ]
            for details in local_files:
                eb = details["path"]
                if eb.suffix.lower() not in web.LINKABLE_EBOOK_EXTENSIONS or not eb.is_file():
                    continue
                fname_lower = eb.name.lower()
                stem_lower = eb.stem.lower()
                if fname_lower in bookorbit_filenames:
                    continue
                metadata = details.get("metadata")
                series = details.get("series")
                if discovery:
                    meta_title, meta_authors = metadata or ("", "")
                    series_name, series_sequence = series or (None, None)
                    inventory_candidates.append(web.EbookResult(
                        name=eb.name,
                        title=meta_title or eb.stem,
                        authors=meta_authors,
                        path=eb,
                        source="Local File",
                        series_name=series_name,
                        series_sequence=series_sequence,
                        file_mtime_ns=details.get("mtime_ns"),
                        file_size=details.get("size"),
                    ))

                # Dedupe: if already found in rich source, skip
                if fname_lower in found_filenames or stem_lower in found_stems:
                    continue

                if not search_term or search_term.lower() in fname_lower:
                    result = web.EbookResult(name=eb.name, path=eb, source='Local File')
                    if discovery:
                        result._local_metadata = metadata or ("", "")
                        result._local_series = series or (None, None)
                    results.append(result)
                    found_filenames.add(fname_lower)
                    found_stems.add(stem_lower)

        except Exception as e:
            if discovery:
                web._record_suggestions_source_state(
                    source_states, "Local File", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                web.logger.warning("⚠️ Filesystem scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                web.logger.warning(f"⚠️ Filesystem search failed: {e}", exc_info=True)

    # Check if we have no sources at all
    if (not results and not web.EBOOK_DIR.exists()
            and not clients.booklore_client.is_configured()
            and not clients.bookorbit_client.is_configured()
            and not web._optional_client_configured(clients, "kavita_client")
            and not clients.bookfusion_client.is_configured()):
        web.get_persistent_condition_logger().warn(
            web.logger,
            "no_ebook_source_configured",
            "⚠️ No ebooks available: No ebook source configured. "
            "Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD), "
            "enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, BOOKORBIT_PASSWORD), "
            "enable Kavita (KAVITA_SERVER, KAVITA_API_KEY), "
            "link BookFusion, "
            "or mount the ebooks directory to /books"
        )

    return results


def _promote_authoritative_ebook_matches(audiobooks, ebooks):
    """Stable-sort ebooks so any whose abs_identifier matches an audiobook source_id rises to the top."""
    if not ebooks or not audiobooks:
        return ebooks
    ab_ids = set()
    for ab in audiobooks:
        sid = getattr(ab, 'source_id', None)
        if sid is not None:
            sid_str = str(sid).strip()
            if sid_str:
                ab_ids.add(sid_str)
    if not ab_ids:
        return ebooks

    def _key(eb):
        ident = getattr(eb, 'abs_identifier', None)
        if ident and str(ident).strip() in ab_ids:
            return 0
        return 1

    ebooks.sort(key=_key)
    return ebooks
