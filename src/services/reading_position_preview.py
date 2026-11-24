"""Resolve a saved BookBridge reading position into a bounded text preview.

This module is intentionally UI-agnostic.  It selects the same per-user progress
state the dashboard considers current, resolves precise ebook locators when they
exist, maps audio time through the stored alignment when necessary, and only then
falls back to an explicitly approximate percentage position.

The returned payload never contains a raw ebook path, locator, or character index.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from bs4 import BeautifulSoup, Tag

from src.utils.ebook_dom_map import content_string_nodes, joined_text, runs_from_nodes, strip_inline_joiner

logger = logging.getLogger(__name__)

_AUDIO_CLIENTS = {"abs", "bookloreaudio", "bookorbitaudio"}
# Write-only trackers never lead a sync, and the percentage stored against them is
# an echo of what BookBridge last pushed there — never an observed reading
# position. They must not be offered as the source of a preview.
_WRITE_ONLY_CLIENTS = {"hardcover", "storygraph", "goodreads"}
_SOURCE_LABELS = {
    "abs": "Audiobookshelf",
    "absebook": "Audiobookshelf (ebook)",
    "kosync": "KoSync",
    "storyteller": "Storyteller",
    "booklore": "Grimmory",
    "bookloreaudio": "Grimmory Audio",
    "bookorbit": "BookOrbit",
    "bookorbitaudio": "BookOrbit Audio",
    "kavita": "Kavita",
    "bookfusion": "BookFusion",
    "cwa": "CWA",
    "readest": "Readest",
}


@dataclass(frozen=True)
class _ResolvedPosition:
    index: int
    status: str
    confidence: str
    detail: str = ""


def _client_key(name: str | None) -> str:
    key = str(name or "").strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "kosync"
    return key


def _source_label(name: str | None) -> str:
    key = _client_key(name)
    if key in _SOURCE_LABELS:
        return _SOURCE_LABELS[key]
    raw = str(name or "").strip()
    return raw or "BookBridge"


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _select_state(states: Iterable, last_leader: str | None):
    states = [
        state for state in (states or [])
        if _client_key(getattr(state, "client_name", None)) not in _WRITE_ONLY_CLIENTS
    ]
    if not states:
        return None

    by_key = {}
    for state in states:
        key = _client_key(getattr(state, "client_name", None))
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None or (_as_float(getattr(state, "last_updated", None)) or 0) > (
            _as_float(getattr(existing, "last_updated", None)) or 0
        ):
            by_key[key] = state

    leader_key = _client_key(last_leader)
    if leader_key and leader_key in by_key:
        return by_key[leader_key]

    # ReadingSession data can be absent for old/imported rows.  In that case the
    # newest observed state is the least surprising display-only fallback.
    # One sync cycle writes several clients with the same last_updated (#523); the
    # client key breaks that tie so the choice never depends on input order.
    return max(
        states,
        key=lambda state: (
            _as_float(getattr(state, "last_updated", None)) or 0,
            _client_key(getattr(state, "client_name", None)),
        ),
    )


def _resolve_filename(book, ebook_parser) -> Optional[str]:
    candidates = []
    for value in (
        getattr(book, "original_ebook_filename", None),
        getattr(book, "ebook_filename", None),
    ):
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    for filename in candidates:
        try:
            ebook_parser.resolve_book_path(filename)
            return filename
        except (FileNotFoundError, OSError):
            continue
    return None


def _resolve_precise_or_mapped_position(
    *,
    state,
    filename: str,
    book,
    ebook_parser,
    alignment_service,
) -> tuple[Optional[_ResolvedPosition], list[str]]:
    failures: list[str] = []

    xpath = str(getattr(state, "xpath", None) or "").strip()
    if xpath:
        index = ebook_parser.resolve_xpath_to_index(filename, xpath)
        if index is not None:
            return _ResolvedPosition(index, "exact", "Exact · XPath"), failures
        failures.append("XPath")

    cfi = str(getattr(state, "cfi", None) or "").strip()
    if cfi.startswith("epubcfi("):
        index = ebook_parser.resolve_cfi_to_index(filename, cfi)
        if index is not None:
            return _ResolvedPosition(index, "exact", "Exact · CFI"), failures
        failures.append("CFI")

    client_key = _client_key(getattr(state, "client_name", None))
    timestamp = _as_float(getattr(state, "timestamp", None))
    if client_key in _AUDIO_CLIENTS and timestamp is not None and alignment_service is not None:
        try:
            index = alignment_service.get_char_for_time(getattr(book, "abs_id", ""), timestamp)
        except Exception:
            logger.warning(
                "Reading position preview: audio alignment lookup failed for %s",
                getattr(book, "abs_id", ""),
                exc_info=True,
            )
            index = None
        if index is not None:
            return _ResolvedPosition(index, "mapped", "Mapped · audio alignment"), failures

    return None, failures


def _clamped_context(context: int) -> int:
    """Clamp the caller's requested context window to the supported range."""
    return max(80, min(int(context), 300))


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _canonical_text(node: BeautifulSoup | Tag) -> str:
    """Reproduce ``EbookParser.extract_text_and_map``'s exact text-extraction
    algorithm for an already-parsed BeautifulSoup document or ``Tag``
    subtree, so it can be compared 1:1 against a slice of ``full_text`` --
    including at an inline (e.g. bionic-reading) word join, which a plain
    ``get_text(separator=' ')`` would not reproduce: it always inserts a
    literal space at every text-node boundary, so ``full_text[start:end]``
    (which may contain ``INLINE_TEXT_JOINER`` instead) would never equal it
    for a spine item with any inline join, silently skipping that spine's
    heading detection below.
    """
    nodes = content_string_nodes(node)
    return joined_text(nodes, runs_from_nodes(nodes))


def _heading_groups(
    full_text: str,
    spine_map: Optional[Iterable[dict]],
    marker_index: int,
    window_start: int,
    window_end: int,
) -> list[tuple[int, int]]:
    """Return unambiguous heading ranges that can safely become line breaks.

    The canonical ebook text deliberately flattens markup.  Preserve only h1-h6
    boundaries whose raw spine XHTML flattens to the exact canonical slice and
    whose heading text occurs exactly once in that slice.  Anything ambiguous is
    left untouched rather than guessing at document structure.

    Only spines overlapping ``window_start``..``window_end`` are parsed.  A
    heading group can only reach the excerpt when it falls ENTIRELY inside one of
    the rendered segments (see :func:`_format_excerpt_segment`), so spines outside
    that window cannot contribute -- and re-parsing every spine's raw XHTML on
    each on-demand preview measured 53-138 ms per request on real library EPUBs
    against 2-10 ms for the window alone.
    """
    groups: list[tuple[int, int]] = []

    for spine in spine_map or []:
        if not isinstance(spine, dict):
            continue
        content = spine.get("content")
        try:
            start = int(spine.get("start"))
            end = int(spine.get("end"))
        except (TypeError, ValueError):
            continue
        if not content or start < 0 or end <= start or end > len(full_text):
            continue
        if end < window_start or start > window_end:
            continue

        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as e:
            logger.debug("Reading position preview: unparsable spine markup skipped: %s", e)
            continue

        spine_text = _normalized_text(_canonical_text(soup))
        if not spine_text or full_text[start:end] != spine_text:
            continue

        spans: list[tuple[int, int]] = []
        for heading in soup.find_all(re.compile(r"^h[1-6]$", re.IGNORECASE)):
            heading_text = _normalized_text(_canonical_text(heading))
            if not heading_text:
                continue

            matches = [match.start() for match in re.finditer(re.escape(heading_text), spine_text)]
            if len(matches) != 1:
                continue

            heading_start = start + matches[0]
            spans.append((heading_start, heading_start + len(heading_text)))

        if not spans:
            continue

        spans.sort()
        group_start, group_end = spans[0]
        for heading_start, heading_end in spans[1:]:
            between = full_text[group_end:heading_start]
            if not between.strip():
                group_end = heading_end
                continue
            if not (group_start <= marker_index <= group_end):
                groups.append((group_start, group_end))
            group_start, group_end = heading_start, heading_end

        if not (group_start <= marker_index <= group_end):
            groups.append((group_start, group_end))

    return groups


def _format_excerpt_segment(
    text: str,
    *,
    segment_start: int,
    heading_groups: Iterable[tuple[int, int]],
) -> str:
    segment_end = segment_start + len(text)
    ranges = [
        (start - segment_start, end - segment_start)
        for start, end in heading_groups
        if segment_start <= start < end <= segment_end
    ]
    if not ranges:
        return re.sub(r"\s+", " ", text)

    parts = []
    cursor = 0
    for start, end in sorted(ranges):
        if start < cursor:
            continue
        parts.append(text[cursor:start])
        parts.append("\n")
        parts.append(text[start:end])
        parts.append("\n")
        cursor = end
    parts.append(text[cursor:])

    formatted = "".join(parts)
    formatted = re.sub(r"[^\S\n]+", " ", formatted)
    formatted = re.sub(r" *\n+ *", "\n", formatted)
    return formatted


def _bounded_excerpt(
    full_text: str,
    index: int,
    context: int,
    heading_groups: Iterable[tuple[int, int]] = (),
) -> tuple[str, str]:
    if not full_text:
        return "", ""
    index = max(0, min(int(index), len(full_text)))
    context = _clamped_context(context)

    before_start = max(0, index - context)
    after_end = min(len(full_text), index + context)
    before = _format_excerpt_segment(
        full_text[before_start:index],
        segment_start=before_start,
        heading_groups=heading_groups,
    )
    after = _format_excerpt_segment(
        full_text[index:after_end],
        segment_start=index,
        heading_groups=heading_groups,
    )

    # Only the OUTER edges are trimmed: stripping the marker-facing edges deletes
    # the space the position sits on, so a boundary renders as
    # "several|notches" and reads as though the marker landed mid-word.
    # strip_inline_joiner runs last so it never disturbs the heading-newline
    # positions _format_excerpt_segment just computed against the raw slice.
    return strip_inline_joiner(before.lstrip()), strip_inline_joiner(after.rstrip())


def unavailable_preview(message: str, *, source: str = "BookBridge", percentage=None) -> dict:
    result = {
        "status": "unavailable",
        "source": source,
        "confidence": "Unavailable",
        "before": "",
        "after": "",
        "message": message,
    }
    if percentage is not None:
        result["percentage"] = round(float(percentage) * 100, 1)
    return result


def build_reading_position_preview(
    *,
    book,
    states: Iterable,
    last_leader: str | None,
    ebook_parser,
    alignment_service=None,
    context_chars: int = 220,
) -> dict:
    """Return a small human-readable preview for one book's current user state.

    Resolution order is deliberately confidence-first:
      XPath -> EPUB CFI -> stored audio alignment -> percentage estimate.

    A failed precise locator never disappears: if percentage fallback is possible
    the payload remains explicitly ``approximate`` and explains that the precise
    locator could not be resolved.
    """
    state = _select_state(states, last_leader)
    if state is None:
        return unavailable_preview("No saved reading position is available for this book.")

    source = _source_label(getattr(state, "client_name", None))
    percentage = _as_float(getattr(state, "percentage", None))
    filename = _resolve_filename(book, ebook_parser)
    if not filename:
        return unavailable_preview(
            "The linked ebook file is not available to resolve this position.",
            source=source,
            percentage=percentage,
        )

    try:
        book_path = ebook_parser.resolve_book_path(filename)
        full_text, spine_map = ebook_parser.extract_text_and_map(book_path)
    except Exception:
        logger.warning(
            "Reading position preview: could not read ebook text for %s",
            getattr(book, "abs_id", ""),
            exc_info=True,
        )
        full_text = ""
        spine_map = []

    if not full_text:
        return unavailable_preview(
            "BookBridge could not read text from the linked ebook.",
            source=source,
            percentage=percentage,
        )

    resolved, precise_failures = _resolve_precise_or_mapped_position(
        state=state,
        filename=filename,
        book=book,
        ebook_parser=ebook_parser,
        alignment_service=alignment_service,
    )

    detail = ""
    if resolved is None and percentage is not None:
        pct = max(0.0, min(1.0, percentage))
        index = int(round(pct * max(0, len(full_text) - 1)))
        if precise_failures:
            detail = (
                f"Stored {' and '.join(precise_failures)} could not be resolved; "
                "showing the saved percentage as an estimate."
            )
        else:
            detail = "No exact ebook locator is available; showing the saved percentage as an estimate."
        resolved = _ResolvedPosition(index, "approximate", "Approximate · percentage", detail)

    if resolved is None:
        message = "No reliable ebook text position can be resolved from the saved state."
        if precise_failures:
            message = f"Stored {' and '.join(precise_failures)} could not be resolved safely."
        return unavailable_preview(message, source=source, percentage=percentage)

    index = max(0, min(int(resolved.index), len(full_text)))
    context = _clamped_context(context_chars)
    heading_groups = _heading_groups(
        full_text, spine_map, index, index - context, index + context
    )
    before, after = _bounded_excerpt(full_text, index, context_chars, heading_groups)
    if not before and not after:
        return unavailable_preview(
            "The saved position resolved, but no surrounding ebook text is available.",
            source=source,
            percentage=percentage,
        )

    payload = {
        "status": resolved.status,
        "source": source,
        "confidence": resolved.confidence,
        "before": before,
        "after": after,
        "message": resolved.detail,
    }
    if percentage is not None:
        payload["percentage"] = round(max(0.0, min(1.0, percentage)) * 100, 1)
    return payload
