"""Canonical series-metadata extraction and resolution.

Shared by the dashboard series grouping, the admin series backfill, and
mapping creation, so every writer of ``books.series_name`` /
``books.series_sequence`` derives them the same way.
"""

import logging
import os
import re
import zipfile
from typing import Any, NamedTuple, Optional, Tuple
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

SeriesTuple = Tuple[Optional[str], Optional[float]]


class SeriesResolution(NamedTuple):
    """Outcome of a series lookup.

    ``service_answered`` records whether a service that owns this book actually
    responded. Only then is "no series" authoritative — a lookup that failed or
    had no configured client must never be read as "the series was removed".
    """

    name: Optional[str]
    sequence: Optional[float]
    source: Optional[str]
    service_answered: bool

_LIBRARY_SEQUENCE_KEYS = ("seriesIndex", "seriesNumber", "seriesSequence")


def _coerce_sequence(raw_seq: Any) -> Optional[float]:
    """Return *raw_seq* as a float, or None when it is missing or unparseable."""
    if raw_seq is None:
        return None
    try:
        return float(raw_seq)
    except (TypeError, ValueError):
        return None


def extract_series_from_abs_metadata(metadata: Any) -> SeriesTuple:
    """Return (series_name, series_sequence) from an ABS ``media.metadata`` block.

    Prefers the structured ``series`` list. The ``seriesName`` fallback is
    decorated ("A Mage's Cultivation #1"), so the trailing ``#N`` is split off
    into the sequence — otherwise it would never group with books resolved from
    the ``series`` list.
    """
    if not isinstance(metadata, dict):
        return None, None

    series_list = metadata.get("series") or []
    if isinstance(series_list, list) and series_list:
        first = series_list[0]
        if isinstance(first, dict):
            name = (first.get("name") or "").strip() or None
            raw_seq = first.get("sequence")
        else:
            name = str(first).strip() or None
            raw_seq = None
        return name, _coerce_sequence(raw_seq)

    name = (metadata.get("seriesName") or "").strip()
    raw_seq = None
    if name:
        decorated = re.match(r"^(.+?)\s+#(\d+(?:\.\d+)?)\s*$", name)
        if decorated:
            name = decorated.group(1).strip()
            raw_seq = decorated.group(2)
    return (name or None), _coerce_sequence(raw_seq)


def extract_series_from_abs_item(item_details: Any) -> SeriesTuple:
    """Return (series_name, series_sequence) from an ABS ``get_item_details`` response."""
    if not isinstance(item_details, dict):
        return None, None
    metadata = item_details.get("media", {}).get("metadata", {}) or {}
    return extract_series_from_abs_metadata(metadata)


def extract_series_from_library_detail(detail: Any) -> SeriesTuple:
    """Return (series_name, series_sequence) from an ebook-library book record.

    Covers BookOrbit, Grimmory (BookLore) and Kavita, which all expose a flat
    ``seriesName`` but spell the index differently. Grimmory nests its fields
    under ``metadata``.
    """
    if not isinstance(detail, dict):
        return None, None

    nested = detail.get("metadata")
    metadata = nested if isinstance(nested, dict) else detail

    name = (metadata.get("seriesName") or "").strip() or None

    # Explicit presence checks, not truthiness: book 0 is a legitimate index.
    raw_seq = None
    for key in _LIBRARY_SEQUENCE_KEYS:
        if metadata.get(key) is not None:
            raw_seq = metadata[key]
            break

    return name, _coerce_sequence(raw_seq)


def extract_series_from_title(title: str) -> SeriesTuple:
    """Return (series_name, series_sequence) parsed out of a title.

    Handles "Series Name N", "Series Name, Book N" and "Series Name (Book N)".
    Returns (None, None) when no clear numeric suffix is found.
    """
    if not title:
        return None, None

    # Strip trailing unabridged/abridged qualifiers
    clean = re.sub(
        r'\s*\((?:unabridged|abridged|audio(?:\s+book)?)\)\s*$',
        '',
        title.strip(),
        flags=re.IGNORECASE,
    )

    # "Title, Book N" / "Title - Book N" / "Title (Book N)"
    m = re.search(
        r'^(.+?)[\s,\-:]+\(?(?:book|volume|vol\.?|part)\s+(\d+(?:\.\d+)?)\)?\s*$',
        clean,
        re.IGNORECASE,
    )
    if m:
        series = m.group(1).rstrip(' ,.!:-').strip()
        if series:
            return series, float(m.group(2))

    # "Title N" — trailing integer (not float, to avoid matching "Author 2.0")
    m = re.match(r'^(.+?)\s+(\d{1,3})\s*$', clean)
    if m:
        series = m.group(1).rstrip(' ,.!:-').strip()
        seq = int(m.group(2))
        # Guard: series candidate must be non-trivially long and seq plausible
        if len(series) >= 4 and 1 <= seq <= 50:
            return series, float(seq)

    return None, None


_CONTAINER_NAMESPACE = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
_CALIBRE_SERIES_META = "calibre:series"
_CALIBRE_SERIES_INDEX_META = "calibre:series_index"
_EPUB3_COLLECTION_PROPERTY = "belongs-to-collection"
_EPUB3_GROUP_POSITION_PROPERTY = "group-position"


def _locate_opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    """Return the archive-relative path to the EPUB's package (OPF) document.

    Reads ``META-INF/container.xml`` for the declared rootfile; falls back to
    the first ``*.opf`` member when the container manifest is missing, empty,
    or unparseable, since some hand-rolled or re-packaged EPUBs omit it.
    """
    opf_path = ""
    try:
        container_xml = zf.read("META-INF/container.xml")
        container_tree = ElementTree.fromstring(container_xml)
        rootfile_el = container_tree.find(".//c:rootfile", _CONTAINER_NAMESPACE)
        if rootfile_el is not None:
            opf_path = (rootfile_el.get("full-path") or "").strip()
    except (KeyError, ElementTree.ParseError):
        opf_path = ""
    if opf_path:
        return opf_path
    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name
    return None


def _local_tag(element: ElementTree.Element) -> str:
    """Return an element's tag without its XML namespace, if any."""
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_calibre_series_from_opf(opf_tree: ElementTree.Element) -> SeriesTuple:
    """Return (series_name, series_sequence) from calibre:series ``<meta>`` elements.

    Matches both namespaced OPF ``meta`` elements and bare ones, since some
    EPUB writers (including some Calibre plugin output) omit the ``opf:``
    namespace prefix.
    """
    name = None
    raw_seq = None
    for meta_el in opf_tree.iter():
        if _local_tag(meta_el) != "meta":
            continue
        meta_name = (meta_el.get("name") or "").strip()
        if meta_name == _CALIBRE_SERIES_META:
            content = (meta_el.get("content") or "").strip()
            if content:
                name = content
        elif meta_name == _CALIBRE_SERIES_INDEX_META:
            raw_seq = meta_el.get("content")
    return name, _coerce_sequence(raw_seq)


def _extract_epub3_collection_from_opf(opf_tree: ElementTree.Element) -> SeriesTuple:
    """Return (series_name, series_sequence) from EPUB3 collection ``<meta>`` properties.

    Secondary form, used when no Calibre ``calibre:series`` meta is present.
    """
    name = None
    raw_seq = None
    for meta_el in opf_tree.iter():
        if _local_tag(meta_el) != "meta":
            continue
        prop = (meta_el.get("property") or "").strip()
        if prop == _EPUB3_COLLECTION_PROPERTY and meta_el.text:
            name = meta_el.text.strip() or None
        elif prop == _EPUB3_GROUP_POSITION_PROPERTY and meta_el.text:
            raw_seq = meta_el.text
    return name, _coerce_sequence(raw_seq)


def extract_series_from_epub(epub_path: Any) -> SeriesTuple:
    """Return (series_name, series_sequence) read from an EPUB's own OPF metadata.

    Fallback for libraries (e.g. CWA/Calibre-Web) whose API/OPDS surface
    carries no series data at all, so the bridge reads the ``calibre:series``
    /``calibre:series_index`` ``<meta>`` elements Calibre writes into the EPUB
    itself. Falls back to the EPUB3 ``belongs-to-collection``/
    ``group-position`` form when no Calibre meta is present.

    Never raises: a missing, corrupt, or non-EPUB file yields (None, None),
    logged at debug (this runs during match/backfill, over many books, so an
    occasional bad file is expected, not exceptional).
    """
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            opf_path = _locate_opf_path(zf)
            if not opf_path:
                return None, None
            try:
                opf_content = zf.read(opf_path)
            except KeyError:
                return None, None
            try:
                opf_tree = ElementTree.fromstring(opf_content)
            except ElementTree.ParseError:
                return None, None

            name, sequence = _extract_calibre_series_from_opf(opf_tree)
            if name:
                return name, sequence
            return _extract_epub3_collection_from_opf(opf_tree)
    except (zipfile.BadZipFile, OSError, ElementTree.ParseError) as e:
        logger.debug("Series resolve: could not read EPUB %s: %s", epub_path, e)
        return None, None
    except Exception as e:
        logger.debug("Series resolve: unexpected error reading EPUB %s: %s", epub_path, e)
        return None, None


def _client_for_source(source_name: Any, clients: dict) -> Optional[Any]:
    """Return the configured library client hosting *source_name*, or None."""
    if not isinstance(source_name, str):
        return None
    normalized = source_name.strip().lower()
    if normalized == "bookorbit":
        client = clients.get("bookorbit_client")
    elif normalized == "booklore":
        client = clients.get("booklore_client")
    elif normalized == "kavita":
        client = clients.get("kavita_client")
    else:
        return None
    if client is None or not client.is_configured():
        return None
    return client


def _library_detail(source_name: Any, client: Any, source_id: Any,
                    force: bool = False) -> Optional[dict]:
    """Fetch the library record that actually carries the series fields.

    BookOrbit's cached light record omits ``seriesName``/``seriesIndex`` and is
    keyed by integer id, so a string ``ebook_source_id`` resolves to nothing
    there; only the detail fetch carries series metadata. Its detail cache holds
    for an hour, so *force* bypasses it when a caller needs to see an edit the
    user just made at the source.
    """
    if client is None or source_id is None or source_id == "":
        return None
    if str(source_name).strip().lower() == "bookorbit":
        if force:
            return client.get_book_detail(source_id, force=True)
        return client.get_book_detail(source_id)
    return client.get_book_by_id(source_id)


def resolve_series_details(
    book: Any,
    *,
    abs_client: Any = None,
    bookorbit_client: Any = None,
    booklore_client: Any = None,
    kavita_client: Any = None,
    ebook_parser: Any = None,
    force_refresh: bool = False,
) -> SeriesResolution:
    """Resolve *book*'s series, reporting which source answered.

    Tries ABS, then the audio library, then the ebook library, then the
    book's own EPUB file, then a title heuristic, stopping at the first
    source that yields a name. The EPUB step sits before the title heuristic
    but after every library client: its OPF metadata is authoritative
    (written by Calibre/an editor), while the title heuristic is a guess, so
    the EPUB must win when both could answer. It exists for services with no
    API path to series data at all (e.g. CWA, whose OPDS feed carries none),
    reading the ``calibre:series`` meta the ebook file already carries on
    disk instead. *book* is duck-typed on ``abs_id``/``abs_title``/
    ``audio_source``/``audio_source_id``/``ebook_source``/``ebook_source_id``/
    ``ebook_filename``, so ORM rows and lightweight namespaces both work.
    Remote lookup failures are logged and skipped, never raised, and leave
    ``service_answered`` False so callers can tell "this book has no series"
    apart from "nobody could tell us".
    """
    abs_id = getattr(book, "abs_id", None)
    abs_title = getattr(book, "abs_title", None)
    audio_source = getattr(book, "audio_source", None)
    audio_source_id = getattr(book, "audio_source_id", None)
    ebook_source = getattr(book, "ebook_source", None)
    ebook_source_id = getattr(book, "ebook_source_id", None)

    clients = {
        "bookorbit_client": bookorbit_client,
        "booklore_client": booklore_client,
        "kavita_client": kavita_client,
    }
    answered = False

    if audio_source == "ABS" and abs_id and abs_client is not None and abs_client.is_configured():
        try:
            item_details = abs_client.get_item_details(abs_id)
            if item_details:
                answered = True
                name, sequence = extract_series_from_abs_item(item_details)
                if name:
                    return SeriesResolution(name, sequence, "abs", True)
        except Exception as e:
            logger.warning(
                f"Series resolve: ABS lookup failed for abs_id={abs_id}: {e}",
                exc_info=True,
            )

    for source, source_id, label in (
        (audio_source, audio_source_id, "audio"),
        (ebook_source, ebook_source_id, "ebook"),
    ):
        if not source or not source_id:
            continue
        client = _client_for_source(source, clients)
        if client is None:
            continue
        try:
            detail = _library_detail(source, client, source_id, force=force_refresh)
            if detail:
                answered = True
                name, sequence = extract_series_from_library_detail(detail)
                if name:
                    return SeriesResolution(name, sequence, str(source).strip().lower(), True)
        except Exception as e:
            logger.warning(
                f"Series resolve: {source} {label} lookup failed for source_id={source_id}: {e}",
                exc_info=True,
            )

    if ebook_parser is not None:
        ebook_filename = getattr(book, "ebook_filename", None)
        if ebook_filename:
            try:
                epub_path = ebook_parser.resolve_book_path(ebook_filename)
            except FileNotFoundError:
                epub_path = None
            except Exception as e:
                logger.warning(
                    f"Series resolve: could not resolve ebook path for '{ebook_filename}': {e}",
                    exc_info=True,
                )
                epub_path = None
            if epub_path is not None and os.path.exists(epub_path):
                name, sequence = extract_series_from_epub(epub_path)
                if name:
                    return SeriesResolution(name, sequence, "epub", True)

    if abs_title:
        name, sequence = extract_series_from_title(abs_title)
        if name:
            return SeriesResolution(name, sequence, "title", answered)

    return SeriesResolution(None, None, None, answered)


def resolve_series_for_book(
    book: Any,
    *,
    abs_client: Any = None,
    bookorbit_client: Any = None,
    booklore_client: Any = None,
    kavita_client: Any = None,
    ebook_parser: Any = None,
    force_refresh: bool = False,
) -> SeriesTuple:
    """Return (series_name, series_sequence) for *book* from the best source available."""
    resolution = resolve_series_details(
        book,
        abs_client=abs_client,
        bookorbit_client=bookorbit_client,
        booklore_client=booklore_client,
        kavita_client=kavita_client,
        ebook_parser=ebook_parser,
        force_refresh=force_refresh,
    )
    return resolution.name, resolution.sequence
