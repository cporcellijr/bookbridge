"""Word map export for device clients.

A reader app that plays the audiobook and shows the ebook needs to know, for any
moment of audio, which word in the EPUB is being spoken. BookBridge already has
that answer: the stored alignment map pairs audio timestamps with character
offsets in ``EbookParser.extract_text_and_map``'s plain text. That character
space is internal to BookBridge, though, so this module translates each map
point into an EPUB CFI that points *inside* a text node (``.../1:15``), the
form Grimmory's web reader and foliate-js resolve.

The existing ``EbookParser._generate_cfi`` is not reused because it always
returns the start of the containing element (``:0``), which is fine for
reading positions but places every word of a paragraph on the paragraph's
first word. The offset-to-node step reuses :mod:`src.utils.ebook_dom_map`, the
same provenance the read-along EPUB builder relies on, so both features agree
on where a character offset lives in the XHTML.
"""
import hashlib
import json
import logging
import threading
from bisect import bisect_right
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, CData, NavigableString, Tag

from src.utils.ebook_dom_map import build_dom_anchor_map, content_string_nodes

if TYPE_CHECKING:
    from src.db.database_service import DatabaseService
    from src.db.models import Book
    from src.services.alignment_service import AlignmentService
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

WORD_MAP_FORMAT_VERSION = 1

_TEXT_TYPES = (NavigableString, CData)


def _element_steps(element: Tag) -> Optional[List[int]]:
    """CFI element steps from the document's root element down to ``element``.

    Each step is ``2 * (position among element siblings, 1-based)``, per the
    EPUB CFI spec. Returns None when ``element`` is not inside an ``<html>``
    root, since a CFI path has nothing to start from.
    """
    chain = []
    current = element
    while isinstance(current, Tag) and current.name != "[document]":
        if current.name == "html":
            break
        chain.append(current)
        current = current.parent
    if not isinstance(current, Tag) or current.name != "html":
        return None

    steps = []
    for node in reversed(chain):
        position = 1
        sibling = node.previous_sibling
        while sibling is not None:
            if isinstance(sibling, Tag):
                position += 1
            sibling = sibling.previous_sibling
        steps.append(position * 2)
    return steps


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units, the unit browser DOM text offsets count in."""
    return len(text.encode("utf-16-le")) // 2


def _text_chunk_base(node: NavigableString) -> Optional[Tuple[str, int]]:
    """``(path, chunk_offset)``: the CFI path to the text chunk holding ``node``,
    and where ``node`` starts inside that chunk, in UTF-16 units.

    A chunk is all the text between two element siblings, so when a parser
    splits it into several string nodes the earlier ones' lengths count.
    Comments are not text in a browser DOM and are skipped.
    """
    parent = node.parent
    if not isinstance(parent, Tag):
        return None
    steps = _element_steps(parent)
    if steps is None:
        return None

    elements_before = 0
    chunk_offset = 0
    counting_chunk = True
    sibling = node.previous_sibling
    while sibling is not None:
        if isinstance(sibling, Tag):
            elements_before += 1
            counting_chunk = False
        elif counting_chunk and type(sibling) in _TEXT_TYPES:
            chunk_offset += _utf16_len(str(sibling))
        sibling = sibling.previous_sibling

    path = "".join(f"/{step}" for step in steps)
    return f"{path}/{2 * elements_before + 1}", chunk_offset


def text_node_cfi_path(node: NavigableString, offset: int) -> Optional[str]:
    """The in-document part of a CFI for ``offset`` characters into ``node``.

    Returns e.g. ``"/4/2/6/1:15"``: element steps to the text's parent, then the
    odd step for the text chunk that holds ``node``, then the offset within that
    chunk. Offsets count UTF-16 units, as browser DOM offsets do, so a character
    outside the Basic Multilingual Plane (most emoji) counts as two.
    """
    base = _text_chunk_base(node)
    if base is None:
        return None
    path, chunk_offset = base
    return f"{path}:{chunk_offset + _utf16_len(str(node)[:max(0, int(offset))])}"


class _ItemIndex:
    """Run lookup for one spine item, built once per export."""

    def __init__(self, item, nodes: List[NavigableString]):
        self.item = item
        self.nodes = nodes
        self.run_starts = [run.start for run in item.runs]


def _locate(items: List[_ItemIndex], item_starts: List[int], offset: int) -> Optional[Tuple[_ItemIndex, int, int]]:
    """Like ``ebook_dom_map.locate_offset``, with precomputed indexes and gap snapping.

    An offset that lands on the space BookBridge inserts between two runs (or
    two spine items) has no XHTML position. Map anchors sit on word starts, so
    such an offset is moved forward to the start of the next run in the same
    spine item; past the item's last run it is dropped.
    """
    i = bisect_right(item_starts, offset) - 1
    if i < 0:
        return None
    index = items[i]
    item = index.item
    if offset >= item.end or not item.runs:
        return None
    j = bisect_right(index.run_starts, offset) - 1
    if j < 0:
        j, offset = 0, item.runs[0].start
    run = item.runs[j]
    if offset >= run.end:
        if j + 1 >= len(item.runs):
            return None
        run = item.runs[j + 1]
        offset = run.start
    return index, run.node_index, run.node_offset_start + (offset - run.start)


def _file_sha256(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def build_word_map(parser: "EbookParser", epub_filename: str, map_points: List[Dict]) -> Dict:
    """Translate alignment ``map_points`` into chapters and in-text CFI paths.

    Returns ``{"chapters": [...], "points": [[ts, chapter, path], ...]}`` with
    points in audio order. ``chapter`` indexes ``chapters``; the full CFI is
    ``epubcfi(/6/{spine_step}!{path})`` using that chapter's ``spine_step``.
    Points that can't be placed in the XHTML (a separator gap at the end of a
    chapter, or a chapter without an ``<html>`` root) are left out and counted
    in ``skipped``.
    """
    dom_map = build_dom_anchor_map(parser, epub_filename)
    _combined, spine_map = parser.extract_text_and_map(epub_filename)
    content_by_index = {entry["spine_index"]: entry["content"] for entry in spine_map}

    items: List[_ItemIndex] = []
    for item in dom_map:
        soup = BeautifulSoup(content_by_index[item.spine_index], "html.parser")
        items.append(_ItemIndex(item, content_string_nodes(soup)))
    item_starts = [index.item.start for index in items]

    chapters: List[Dict] = []
    chapter_number: Dict[int, int] = {}
    points: List[list] = []
    skipped = 0
    previous: Optional[Tuple[int, str]] = None
    # Points in one text node share its chunk path, and finding that path walks
    # the node's earlier siblings: per point, that is quadratic in a long file.
    # Values are (chunk_path, chunk_offset, text when it has non-BMP characters).
    node_bases: Dict[Tuple[int, int], Optional[Tuple[str, int, Optional[str]]]] = {}

    ordered = sorted(map_points, key=lambda p: float(p.get("ts", 0.0)))
    for point in ordered:
        char = point.get("global_char", point.get("char"))
        if char is None:
            skipped += 1
            continue
        located = _locate(items, item_starts, int(char))
        if located is None:
            skipped += 1
            continue
        index, node_index, node_offset = located
        if node_index >= len(index.nodes):
            skipped += 1
            continue
        spine_index = index.item.spine_index
        key = (spine_index, node_index)
        if key not in node_bases:
            text = str(index.nodes[node_index])
            base = _text_chunk_base(index.nodes[node_index])
            astral = text if _utf16_len(text) != len(text) else None
            node_bases[key] = (base[0], base[1], astral) if base else None
        base = node_bases[key]
        if base is None:
            skipped += 1
            continue
        chunk_path, chunk_offset, astral = base
        units = _utf16_len(astral[:node_offset]) if astral else node_offset
        path = f"{chunk_path}:{chunk_offset + units}"
        if spine_index not in chapter_number:
            chapter_number[spine_index] = len(chapters)
            chapters.append({
                "href": index.item.href,
                "spine_index": spine_index,
                "spine_step": spine_index * 2,
            })
        number = chapter_number[spine_index]
        # Interpolated maps can repeat a position; one point per spot is enough.
        if previous == (number, path):
            continue
        previous = (number, path)
        points.append([round(float(point.get("ts", 0.0)), 3), number, path])

    return {"chapters": chapters, "points": points, "skipped": skipped}


class WordMapService:
    """Builds and caches device word maps, one per book and map version."""

    def __init__(
        self,
        database_service: "DatabaseService",
        alignment_service: "AlignmentService",
        ebook_parser: "EbookParser",
        cache_size: int = 4,
    ) -> None:
        self.database_service = database_service
        self.alignment_service = alignment_service
        self.ebook_parser = ebook_parser
        self._cache: "OrderedDict[tuple, Dict]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    def status(self, book: "Book") -> Dict:
        """Where matching stands for ``book`` when no map is stored yet."""
        job = self.database_service.get_latest_job(book.abs_id)
        result = {"status": "not_ready", "book_status": getattr(book, "status", None)}
        if job is not None:
            result["progress"] = round(float(job.progress or 0.0), 3)
            if getattr(job, "stage", None):
                result["stage"] = job.stage
            if getattr(job, "last_error", None):
                result["last_error"] = job.last_error
        return result

    def get_word_map(self, book: "Book", epub_filename: Optional[str]) -> Tuple[int, Dict]:
        """Return ``(http_status, body)`` for a device's word-map request."""
        if getattr(book, "sync_mode", None) == "ebook_only":
            return 409, {"status": "no_audio", "error": "This book has no audiobook in BookBridge"}
        row = self.database_service.get_alignment_meta(book.abs_id)
        if row is None:
            return 202, self.status(book)
        if not epub_filename:
            return 409, {"status": "no_ebook", "error": "This book has no ebook file in BookBridge"}

        map_version = row["last_updated"].isoformat() if row["last_updated"] else None
        key = (book.abs_id, map_version, epub_filename)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return 200, cached

            map_points = self.alignment_service._get_alignment(book.abs_id) or []
            unsupported = {"status": "unsupported", "error": "This book's text could not be mapped to its EPUB"}
            try:
                exported = build_word_map(self.ebook_parser, epub_filename, map_points)
                book_path = self.ebook_parser.resolve_book_path(epub_filename)
            except (ValueError, OSError) as e:
                # The EPUB is missing, or build_dom_anchor_map refused to guess
                # because its replay of the text extraction disagreed.
                logger.warning("Word map export failed for '%s': %s", book.abs_id, e, exc_info=True)
                return 409, unsupported
            if not exported["points"]:
                # extract_text_and_map answers an unreadable EPUB with empty text.
                logger.warning("Word map export placed no words for '%s'", book.abs_id)
                return 409, unsupported

            # A Grimmory-audio book often has no duration on record (0 or None).
            # The last matched word is then the best lower bound available.
            duration = getattr(book, "audio_duration", None) or getattr(book, "duration", None)
            if not duration and exported["points"]:
                duration = exported["points"][-1][0]
            duration = duration or None
            body = {
                "format": WORD_MAP_FORMAT_VERSION,
                "abs_id": book.abs_id,
                "status": "ready",
                "map_version": map_version,
                "align_method": row["align_method"],
                "quality_score": row["quality_score"],
                "duration": duration,
                "epub": {
                    "filename": Path(str(epub_filename)).name,
                    "sha256": _file_sha256(Path(book_path)) if book_path else None,
                },
                **exported,
            }
            self._cache[key] = body
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return 200, body


def word_map_etag(body: Dict) -> str:
    """A stable validator for a word map: its map version plus the EPUB hash."""
    basis = json.dumps([body.get("abs_id"), body.get("map_version"), (body.get("epub") or {}).get("sha256"),
                        WORD_MAP_FORMAT_VERSION])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
