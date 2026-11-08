"""DOM-anchored text extraction for read-along EPUB 3 generation (Phase 1).

``EbookParser.extract_text_and_map`` (``src/utils/ebook_utils.py``) builds a book's
plain-text representation with ``BeautifulSoup(item.get_content(), 'html.parser')``
then ``soup.get_text(separator=' ', strip=True)``. Alignment maps (the bridge's
transcript-to-audio-timestamp maps) live in that exact character space. Generating
SMIL media overlays requires the reverse: given a character offset in that space,
find the source text node and offset inside the *original* spine-item XHTML so a
marker can be inserted there.

``strip=True`` is not invertible from the joined string alone, so this module does
not try to reverse-engineer it. Instead it walks the same DOM tree
``extract_text_and_map`` walked and replicates bs4's own per-node algorithm
directly, recording provenance as it goes:

    bs4's ``Tag._all_strings(strip=True)`` (what ``get_text`` calls) walks
    ``soup.descendants``, keeps only nodes whose *exact* type is
    ``NavigableString`` or ``CData`` (this is what silently drops ``<script>``/
    ``<style>`` content -- those are the distinct ``Script``/``Stylesheet``
    subclasses -- and comments/doctype/processing-instructions), strips each
    surviving string individually, and drops it entirely if stripping empties it.
    ``get_text(separator=' ')`` then joins the surviving *already-stripped*
    strings with a single space. It does NOT join first and strip the result --
    verified against the exact bs4 build pinned in this repo
    (``.venv/Lib/site-packages/bs4/element.py``, ``Tag._all_strings``).

This module re-parses each spine item's *already-captured* ``content`` bytes from
``extract_text_and_map``'s own ``spine_map`` (rather than re-reading the EPUB), so
it can never drift from the exact bytes the reference text was built from, and it
never touches ``EbookParser.cache`` -- it is an ordinary reader of
``extract_text_and_map``'s cached-or-fresh result, exactly like the interface's
other 28 callers.

Node identity is a 0-based index into the *content-string* list for that spine
item (i.e. the same nodes ``get_text`` would enumerate, in document order, before
the empty-after-strip ones are dropped). Re-parsing the same ``content`` bytes
with the same parser is deterministic, so that index is a stable, reproducible
way to relocate the node later (e.g. to insert a SMIL marker span in Phase 3) --
no fragile CSS-selector or XPath scheme required.
"""
import html.entities
import logging
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union, TYPE_CHECKING

from bs4 import BeautifulSoup, CData, NavigableString, Tag

if TYPE_CHECKING:
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

# The exact set bs4's Tag._all_strings() filters to when called (indirectly, via
# get_text()) on a top-level BeautifulSoup document, whose own
# `interesting_string_types` is unset and therefore resolves to this constant.
# Kept as our own copy (rather than reaching into `soup.interesting_string_types`
# per call) so behaviour is pinned regardless of a given document's tag names.
_CONTENT_STRING_TYPES = (NavigableString, CData)

# Tags whose text content bs4's HTML-mode builders never surface as ordinary
# content strings (they get the special ``Script``/``Stylesheet`` subclasses
# instead, which fail the exact-type check above -- see this module's
# docstring). ``TreeBuilder.string_containers`` only carries that mapping for
# HTML-flavoured builders; the XML builder :func:`parse_original_spine_xml`
# uses for read-along fidelity (Finding 1) inherits the base class's *empty*
# mapping, so without this explicit parent-tag check, ``<script>``/``<style>``
# text would leak into the node list when parsing a spine item's ORIGINAL
# archive bytes as XML, even though it never did when parsing the (lossy)
# ebooklib-reconstructed content ``extract_text_and_map`` builds ``combined_text``
# from. Filtering by parent tag name is redundant (but harmless) for the HTML
# path, since those nodes are already excluded there by type; it is load-bearing
# for the XML path.
_EXCLUDED_STRING_PARENTS = frozenset({"script", "style", "template", "head"})

# Elements whose edges end a read-along sentence regardless of punctuation.
# Credit lines, headings and captions often carry no terminal punctuation, so
# the punctuation-only splitter merged them with the next paragraph into one
# "sentence" whose marker could only wrap its first block -- the rest of the
# lines were never highlighted. Storyteller segments per block element too.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "caption", "dd",
    "div", "dl", "dt", "figcaption", "figure", "footer", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hgroup", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "td", "th", "tr", "ul",
})

# A single character substituted for the separator ``extract_text_and_map``
# would otherwise insert between two adjacent content-string runs, when (and
# only when) that separator is a mid-word split introduced by "bionic
# reading" EPUB markup -- e.g. ``<b>Th</b>e``, which bs4's
# ``get_text(separator=' ')`` turns into ``Th e``. Kept at exactly one
# character so every downstream offset (xpath/CFI generation, chapter
# percentage math, spine ``char_len``) stays the width it always was: the
# joiner REPLACES the separator rather than removing it. U+2063 INVISIBLE
# SEPARATOR is a Unicode format character with no glyph, is not
# alphanumeric (so ``EbookParser._normalize_with_map``'s ``ch.isalnum()``
# filter already drops it for free) and is not whitespace (so it cannot be
# mistaken for a real word boundary by a naive ``.split()``) -- and Unicode
# reserves it for exactly this purpose ("used to indicate word or morpheme
# boundaries ... in situations where the use of a visible word divider is
# not desired"). It must never reach anything outside this module and
# :class:`~src.utils.ebook_utils.EbookParser` -- see :func:`strip_inline_joiner`.
INLINE_TEXT_JOINER = "\u2063"

# Inline tags whose only rendering effect is character-level styling, with no
# content or structural meaning of their own. "Bionic reading" tools split a
# single word's characters across runs of exactly these tags with no literal
# whitespace at the split, which is the one case ``extract_text_and_map``
# should reunite with :data:`INLINE_TEXT_JOINER` instead of a real space.
#
# Deliberately narrow. ``span``/``a``/``sub``/``sup`` are excluded on purpose:
# they routinely mark a REAL word boundary with no source whitespace either --
# verse lines as sibling ``<span class="line">``, or a footnote reference as
# ``word<a><sup>1</sup></a>`` -- and joining those fuses two words that must
# stay separate (an alignment map built from the joined text would then anchor
# on "word1" or "linesecond", which never occurs in the narrated audio).
# Restricting the join to this list means ``<a>``/``<sup>``/``<sub>`` (and any
# ``epub:type="noteref"`` marker, which is always carried on one of those, not
# on a bare ``<b>``/``<i>``) can never join, without needing a separate
# noteref-specific check.
INLINE_JOIN_SAFE_TAGS = frozenset({"b", "strong", "i", "em", "u"})

# XML's five predefined entities -- the only named entity references a strict
# XML parser understands without an external/internal DTD. Left untouched by
# :func:`escape_named_html_entities` (rewriting ``&amp;`` to ``&#38;`` would be
# harmless in isolation, but the point of that function is to touch only the
# entities a bare XML parser cannot already resolve).
_XML_BUILTIN_ENTITY_NAMES = frozenset({"amp", "lt", "gt", "apos", "quot"})

# A named entity reference, e.g. ``&nbsp;`` or ``&mdash;``.
_NAMED_ENTITY_RE = re.compile(r'&([a-zA-Z][a-zA-Z0-9]*);')


@dataclass(frozen=True)
class DomRun:
    """One emitted, non-empty-after-strip text run, with source-node provenance.

    ``start``/``end`` are global character offsets in the same character space as
    ``EbookParser.extract_text_and_map``'s combined text (half-open ``[start,
    end)``). ``node_index`` is this run's source node's position (0-based) in the
    spine item's content-string node list, in document order. ``node_offset_start``/
    ``node_offset_end`` are the half-open offset range within that node's *original*
    (unstripped) string value that produced ``text``.
    """
    node_index: int
    node_offset_start: int
    node_offset_end: int
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class SpineDomMap:
    """DOM-anchored runs for one spine item, aligned with an ``extract_text_and_map``
    ``spine_map`` entry.

    ``spine_index``, ``href``, ``start`` and ``end`` mirror the corresponding
    ``spine_map`` dict fields exactly (``start``/``end`` are the same half-open
    global range). ``runs`` is empty for a spine item with no content-string nodes,
    or one whose nodes all strip to empty. ``node_count`` is the total number of
    content-string nodes considered for this item (including ones that stripped to
    empty and so produced no run) -- an upper bound for validating a ``node_index``.
    ``text`` is this item's slice of ``extract_text_and_map``'s combined text
    (``combined_text[start:end]``), already verified equal to the rebuilt
    :func:`joined_text` of ``runs`` by :func:`build_dom_anchor_map` -- stored
    rather than re-derived so :func:`reconstruct_text` needs no join logic of
    its own.
    """
    spine_index: int
    href: str
    start: int
    end: int
    runs: List[DomRun] = field(default_factory=list)
    node_count: int = 0
    text: str = ""


def content_string_nodes(soup: BeautifulSoup) -> List[NavigableString]:
    """All descendants of ``soup`` that ``soup.get_text()`` would enumerate.

    Public because the read-along builder re-parses the same ``content`` bytes to
    splice in markers and must enumerate nodes in exactly the order
    :func:`locate_offset` indexed them. Re-deriving this filter there would risk a
    silent divergence that misplaces every marker in the item.

    Mirrors ``Tag._all_strings`` filtering to the exact types ``get_text()``
    considers for a top-level document (excludes ``Comment``, ``Doctype``,
    ``ProcessingInstruction``, and the ``Script``/``Stylesheet``/``TemplateString``
    subclasses used for ``<script>``/``<style>``/``<template>`` content -- all are
    ``NavigableString`` subclasses but not of the exact filtered types), PLUS an
    explicit parent-tag-name check for the same three tags (see
    :data:`_EXCLUDED_STRING_PARENTS`) -- a no-op for an HTML-flavoured parser
    (those nodes are already excluded by type there) but load-bearing when
    ``soup`` was built by the XML builder :func:`parse_original_spine_xml` uses,
    which has no HTML-specific string-container knowledge and would otherwise
    surface ``<script>``/``<style>`` text as ordinary content.
    """
    nodes = []
    for node in soup.descendants:
        if type(node) not in _CONTENT_STRING_TYPES:
            continue
        if _has_excluded_ancestor(node):
            continue
        nodes.append(node)
    return nodes


def _has_excluded_ancestor(node: NavigableString) -> bool:
    """Whether ``node`` sits under a tag whose text never reaches the canonical
    character space.

    ``<script>``/``<style>``/``<template>`` are excluded for the reason given in
    :func:`content_string_nodes`. ``<head>`` is excluded for a different and
    less obvious one: ebooklib's reconstruction, which is what
    ``extract_text_and_map`` builds ``combined_text`` from, empties the head
    outright -- a real book's

        <head><link rel="stylesheet" .../><title>c2T</title></head>

    comes back as ``<head/>``. So the canonical text contains no ``<title>``
    text, while parsing the ORIGINAL archive bytes does surface it. Left
    unfiltered that put four extra characters (``"c2T "``) at offset 0 of the
    affected spine item, shifting every marker in it and tripping the builder's
    own injection self-check. That discarded head is also where the stylesheet
    links live, which is the root of the styling loss this XML path exists to
    repair.
    """
    parent = node.parent
    while parent is not None:
        name = getattr(parent, "name", None)
        if name and name.lower() in _EXCLUDED_STRING_PARENTS:
            return True
        parent = parent.parent
    return False


def runs_from_nodes(nodes: List[NavigableString]) -> List[DomRun]:
    """Build local-offset :class:`DomRun`\\ s from an already-enumerated content-
    string node list, in the same order/offset scheme :func:`_spine_item_runs`
    uses.

    Public and separated from parsing so the read-along builder can run this
    exact algorithm a second time over a spine item's ORIGINAL archive bytes
    (parsed with :func:`parse_original_spine_xml` rather than ebooklib's lossy
    reconstruction) and get directly comparable ``node_offset_start``/
    ``node_offset_end`` values -- those are computed fresh from each node's own
    (unstripped) string, so they are only ever valid relative to the exact
    node list they were built from. Reusing offsets computed against one
    document's nodes against a *different* document's (even structurally
    equivalent) nodes would silently misplace a marker if the two documents'
    whitespace serialization differs, which is why this is a function on a node
    list rather than something baked into the offsets stored anywhere.
    """
    runs: List[DomRun] = []
    local_idx = 0
    for node_index, node in enumerate(nodes):
        raw = str(node)
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        node_offset_start = leading
        node_offset_end = leading + len(stripped)

        if runs:
            local_idx += 1  # the single-space separator emitted before this run
        start = local_idx
        end = start + len(stripped)
        runs.append(DomRun(
            node_index=node_index,
            node_offset_start=node_offset_start,
            node_offset_end=node_offset_end,
            text=stripped,
            start=start,
            end=end,
        ))
        local_idx = end
    return runs


def _nearest_join_boundary(node: NavigableString) -> Optional[Tag]:
    """The nearest ancestor of ``node`` that is not a pure character-styling
    tag (see :data:`INLINE_JOIN_SAFE_TAGS`).

    Two adjacent runs are considered the same "typographic run" -- i.e. two
    fragments of one word split by inline styling markup -- exactly when this
    returns the identical :class:`Tag` object (by identity, not just name) for
    both of them: the climb from each node stops the moment it leaves the
    chain of safe styling tags, so it lands on the first shared real content
    container only if both nodes are reachable from it through nothing but
    styling. Two separate ``<span>`` (or ``<a>``, ``<sup>``, ``<sub>``, ...)
    siblings never share this ancestor, because the climb from each stops at
    its own span/anchor/sup, not the container both spans sit in.
    """
    parent = node.parent
    while parent is not None:
        name = getattr(parent, "name", None)
        if not name or name.lower() not in INLINE_JOIN_SAFE_TAGS:
            return parent
        parent = parent.parent
    return None


def _has_literal_whitespace_between(nodes: List[NavigableString], prev_index: int, next_index: int) -> bool:
    """Whether the source markup had a real whitespace character between the
    content-string nodes at ``prev_index`` and ``next_index`` in ``nodes``
    (which need not be adjacent -- any node stripped to empty in between,
    always whitespace-only, counts as one), i.e. whether bs4's own
    ``get_text(separator=' ')`` output would have had a literal space there
    regardless of the separator it inserts.
    """
    if next_index > prev_index + 1:
        return True  # a node (necessarily whitespace-only, or it would survive) was skipped
    prev_raw = str(nodes[prev_index])
    if prev_raw and prev_raw[-1].isspace():
        return True
    next_raw = str(nodes[next_index])
    if next_raw and next_raw[0].isspace():
        return True
    return False


def _has_element_break_between(prev_node: NavigableString, next_node: NavigableString) -> bool:
    """Whether any element other than a pure styling tag (or ``<wbr>``) opens
    between ``prev_node`` and ``next_node`` in document order.

    Void elements such as ``<br/>`` and ``<img/>`` produce no content string of
    their own, so :func:`_nearest_join_boundary` alone cannot see them:
    ``first line<br/>second line`` would otherwise fuse into one word.
    ``<wbr>`` marks a word-break *opportunity* with no rendered space, so it
    does not count as a break.
    """
    for element in prev_node.next_elements:
        if element is next_node:
            return False
        if isinstance(element, Tag):
            name = (element.name or "").lower()
            if name not in INLINE_JOIN_SAFE_TAGS and name != "wbr":
                return True
    return False


def joined_text(nodes: List[NavigableString], runs: List[DomRun]) -> str:
    """Reconstruct a spine item's text from ``runs``, using
    :data:`INLINE_TEXT_JOINER` in place of the usual single-space separator
    wherever two adjacent runs are a mid-word inline split (see
    :func:`_nearest_join_boundary`) with no literal whitespace between them.

    The one canonical join decision, called from both
    ``EbookParser.extract_text_and_map`` (via
    ``EbookParser._extract_text_from_soup``) and this module's own
    :func:`_spine_item_runs` / :func:`block_break_offsets` -- so the DOM-anchored
    read-along map's rebuilt text always matches ``extract_text_and_map``'s
    reference text exactly, including at inline joins, instead of only when
    the book happens to have none.

    Every boundary still contributes exactly one separator character (space or
    joiner), matching :func:`runs_from_nodes`'s offset arithmetic, which does
    not (and does not need to) know which of the two was used.
    """
    if not runs:
        return ""
    parts: List[str] = [runs[0].text]
    previous_boundary = _nearest_join_boundary(nodes[runs[0].node_index])
    for position in range(1, len(runs)):
        run = runs[position]
        current_boundary = _nearest_join_boundary(nodes[run.node_index])
        if _has_literal_whitespace_between(nodes, runs[position - 1].node_index, run.node_index):
            separator = " "
        elif (
            previous_boundary is not None
            and previous_boundary is current_boundary
            and not _has_element_break_between(nodes[runs[position - 1].node_index], nodes[run.node_index])
        ):
            separator = INLINE_TEXT_JOINER
        else:
            separator = " "
        parts.append(separator)
        parts.append(run.text)
        previous_boundary = current_boundary
    return "".join(parts)


def strip_inline_joiner(text: Optional[str]) -> Optional[str]:
    """Remove :data:`INLINE_TEXT_JOINER` from ``text``.

    Every return value, log line, or search haystack that leaves this module
    and :class:`~src.utils.ebook_utils.EbookParser` must be passed through this
    (or :func:`strip_inline_joiner_with_map`, when the caller also needs to
    convert a match position back into raw-text offsets) -- the joiner exists
    only to keep ``extract_text_and_map``'s character-offset math stable while
    a book has bionic-reading mid-word splits; no other consumer should ever
    see it.
    """
    if not text:
        return text
    return text.replace(INLINE_TEXT_JOINER, "")


def strip_inline_joiner_with_map(text: str) -> Tuple[str, List[int]]:
    """Like :func:`strip_inline_joiner`, but also returns a list mapping each
    character index of the stripped text back to its index in ``text``.

    For exact-match text search (substring ``find``, uniqueness-by-``count``
    anchors) against ``full_text``: a search phrase sourced from
    ``get_text_at_percentage`` or similar is already joiner-free, so it can
    only ever match the joiner-free view of ``full_text``. The returned map
    converts a match position found there back into ``full_text``'s own
    character space, which is what spine-item bounds, percentage math, and
    xpath/CFI generation all assume.
    """
    if INLINE_TEXT_JOINER not in text:
        return text, list(range(len(text)))
    chars: List[str] = []
    index_map: List[int] = []
    for raw_idx, ch in enumerate(text):
        if ch == INLINE_TEXT_JOINER:
            continue
        chars.append(ch)
        index_map.append(raw_idx)
    return "".join(chars), index_map


def _spine_item_runs(content: Union[str, bytes]) -> Tuple[List[DomRun], int, str]:
    """Build provenance-carrying runs for one spine item's raw XHTML ``content``.

    Returns ``(runs, node_count, item_text)`` where ``item_text`` is
    :func:`joined_text` of ``runs`` -- the reconstruction of what
    ``EbookParser.extract_text_and_map`` would have produced for the same
    ``content``. Offsets in the returned runs are *local* to this item (0-based);
    the caller shifts them into the book's global char space.
    """
    soup = BeautifulSoup(content, 'html.parser')
    nodes = content_string_nodes(soup)
    runs = runs_from_nodes(nodes)
    item_text = joined_text(nodes, runs)
    return runs, len(nodes), item_text


def _block_ancestor(node: NavigableString) -> Optional[Tag]:
    """The nearest enclosing :data:`_BLOCK_TAGS` element of ``node``, or ``None``."""
    parent = node.parent
    while parent is not None:
        name = getattr(parent, "name", None)
        if name and name.lower() in _BLOCK_TAGS:
            return parent
        parent = parent.parent
    return None


def block_break_offsets(content: Union[str, bytes], expected_text: str) -> Optional[List[int]]:
    """Local offsets in one spine item's text where a new block element begins.

    Each offset is the start of a text run whose nearest block ancestor differs
    from the previous run's, in the same local character space as
    :func:`_spine_item_runs` (and so as ``extract_text_and_map``'s slice for
    this item). Returns ``None`` when the rebuilt text does not equal
    ``expected_text`` -- offsets computed against drifted text would cut
    sentences in the wrong place, so the caller falls back to punctuation-only
    splitting.
    """
    soup = BeautifulSoup(content, 'html.parser')
    nodes = content_string_nodes(soup)
    runs = runs_from_nodes(nodes)
    if joined_text(nodes, runs) != expected_text:
        return None

    breaks: List[int] = []
    previous_block: Optional[Tag] = None
    for position, run in enumerate(runs):
        block = _block_ancestor(nodes[run.node_index])
        if position > 0 and block is not previous_block:
            breaks.append(run.start)
        previous_block = block
    return breaks


def _resolve_named_entity(match: "re.Match") -> str:
    """Replace one named HTML entity reference with numeric character
    reference(s) an XML parser can resolve without a DTD.

    A bare XML parser only understands the five entities in
    :data:`_XML_BUILTIN_ENTITY_NAMES`; everything else (``&nbsp;``, ``&mdash;``,
    ``&hellip;`` ...) requires either network/DTD resolution (which
    :func:`parse_original_spine_xml` deliberately never does) or pre-conversion
    to the character it names. Real-world EPUB content documents -- especially
    ones produced by tools like ``pdftohtml`` -- routinely use these bare,
    un-substituted, without declaring them in an internal DTD subset. Left
    unresolvable names untouched (returns the original match) rather than
    guessing; the caller's own text-equality verification against
    ``extract_text_and_map``'s combined text will catch the resulting parse
    failure or mismatch and fall back to the lossy reconstructed-content path
    for that one spine item.
    """
    name = match.group(1)
    if name in _XML_BUILTIN_ENTITY_NAMES:
        return match.group(0)
    resolved = html.entities.html5.get(name + ";")
    if resolved is None:
        return match.group(0)
    return "".join(f"&#{ord(ch)};" for ch in resolved)


def escape_named_html_entities(content: bytes) -> bytes:
    """Rewrite named HTML entity references in ``content`` to numeric character
    references, leaving XML's five predefined entities alone.

    Read-along fidelity (Finding 1 of the independent review) requires parsing
    a spine item's ORIGINAL archive bytes with a case- and attribute-preserving
    **XML** parser rather than the HTML-mode parser ``extract_text_and_map``
    uses -- HTML mode is what silently lowercases XML-cased attributes like
    ``viewBox``. A bare XML parser, though, only knows the five entities in
    :data:`_XML_BUILTIN_ENTITY_NAMES` unless it resolves an external/internal
    DTD; real EPUB content documents routinely use other named entities
    (``&nbsp;``, ``&mdash;``, ...) without declaring them. This pre-pass makes
    those parseable without ever contacting a network for a DTD.

    ``content`` is decoded as UTF-8 (EPUB content documents are required to be
    UTF-8 or UTF-16, and this repo already treats spine content as UTF-8
    elsewhere) with ``errors='replace'`` so a decoding hiccup degrades rather
    than raises here -- the caller's downstream text-equality check is what
    actually gates whether the result is trustworthy.
    """
    text = content.decode("utf-8", "replace")
    text = _NAMED_ENTITY_RE.sub(_resolve_named_entity, text)
    return text.encode("utf-8")


_BODY_START_RE = re.compile(rb"<body[\s>/]")
# Markup whose bytes must pass through untouched: in CDATA, comments and PIs a
# character reference is literal text (``&#13;`` would corrupt an SVG <style>).
_MARKUP_RE = re.compile(
    rb"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.S
)


def _preserve_carriage_returns(content: bytes) -> bytes:
    """Keep the body's carriage returns through the XML parse, as ``&#13;``.

    XML end-of-line handling turns every CRLF into LF, but ebooklib's HTML-mode
    reconstruction, whose text the sentence offsets are counted in, keeps the CR.
    A CRLF source therefore never matched run for run, and every spine item was
    refused (diagnostics finding 9177). A character reference survives the
    parse. Only ordinary character data after ``<body`` is touched: one in the
    prolog is not well-formed XML, and markup (see ``_MARKUP_RE``) is copied as is.
    """
    if b"\r" not in content:
        return content
    body = _BODY_START_RE.search(content)
    if body is None:
        return content
    out, pos = [content[:body.start()]], body.start()
    for markup in _MARKUP_RE.finditer(content, body.start()):
        out.append(content[pos:markup.start()].replace(b"\r", b"&#13;"))
        out.append(markup.group(0))
        pos = markup.end()
    out.append(content[pos:].replace(b"\r", b"&#13;"))
    return b"".join(out)


def parse_original_spine_xml(content: bytes) -> Optional[BeautifulSoup]:
    """Parse a spine item's ORIGINAL (un-reconstructed) archive bytes with a
    case- and attribute-preserving XML parser.

    ``extract_text_and_map`` (and this module's own :func:`_spine_item_runs`)
    parse ``item.get_content()`` -- ebooklib's own from-scratch reconstruction
    of the document, built by re-parsing the original bytes in **HTML** mode
    (which lowercases every tag/attribute name, since HTML is case-insensitive)
    and discarding the original ``<head>``'s stylesheet links and the original
    ``<body>``'s own attributes (see ``EpubHtml.get_content``). None of that
    reconstruction is fit to ship to a reader -- Finding 1 of the independent
    review of this feature. This function instead parses the ORIGINAL bytes
    with bs4's ``lxml-xml`` builder, which is namespace-aware and preserves
    case exactly, so ``viewBox``/``linearGradient``/etc. survive untouched.

    Returns ``None`` -- never raises -- on any parse failure (including a
    document that is not well-formed XML at all, which some real-world "XHTML"
    content documents are not): the caller falls back to the lossy
    reconstructed-content path for that one spine item rather than risk
    injecting a marker at a node index computed against a document structure
    that may not correspond to this one.
    """
    try:
        return BeautifulSoup(escape_named_html_entities(_preserve_carriage_returns(content)), "lxml-xml")
    except Exception as e:
        logger.warning(
            "Could not parse original spine XML for read-along fidelity: %s",
            e, exc_info=True,
        )
        return None


def original_body_scope(soup: BeautifulSoup) -> Union[BeautifulSoup, Tag]:
    """The ``<body>`` element of ``soup``, or ``soup`` itself if none is found.

    Content-string node enumeration for the ORIGINAL-bytes path is scoped to
    ``<body>`` (rather than the whole document, as ``extract_text_and_map``'s
    HTML-mode parse effectively is) because a spine item read via
    ``ebooklib.epub.read_epub`` always has ``item.title == ""`` (nothing in the
    reader ever sets it per-item), so ``EpubHtml.get_content()``'s reconstructed
    ``<head>`` never contains a ``<title>`` -- its only contents are self-closing
    ``<meta>``/``<link>`` tags, which contribute zero content-string nodes.
    The reconstructed document's node list is therefore already exactly its
    ``<body>``'s node list; scoping the original parse the same way keeps the
    two directly comparable by node index, and additionally sidesteps the
    original document's own (possibly real) ``<title>`` text, which would
    otherwise shift every subsequent node index.
    """
    body = soup.find("body")
    return body if body is not None else soup


def build_dom_anchor_map(parser: "EbookParser", filepath: Union[str, Path]) -> List[SpineDomMap]:
    """Build the DOM-anchored run map for an EPUB, spine item by spine item.

    Calls ``parser.extract_text_and_map(filepath)`` to get the reference combined
    text and ``spine_map`` (an ordinary cached call, identical to any of that
    method's other callers -- this never bypasses or invalidates its cache), then
    re-parses each spine item's already-captured ``content`` bytes to recover
    per-run node provenance. Raises ``ValueError`` if a rebuilt item's text does
    not match the corresponding slice of the reference combined text exactly --
    that would mean this module's replication of bs4's algorithm has drifted from
    the reference implementation, and silently returning wrong provenance would
    misplace every downstream SMIL marker for that item.

    :param parser: the ``EbookParser`` instance to source book text/spine data from.
    :param filepath: path to the EPUB, exactly as accepted by
        ``EbookParser.extract_text_and_map``.
    :return: one ``SpineDomMap`` per spine item present in ``extract_text_and_map``'s
        ``spine_map`` (spine entries it skips -- missing manifest item, non-document
        type -- are absent here too, identically).
    """
    combined_text, spine_map = parser.extract_text_and_map(filepath)

    dom_maps: List[SpineDomMap] = []
    for entry in spine_map:
        runs, node_count, item_text = _spine_item_runs(entry["content"])
        start = entry["start"]
        end = entry["end"]

        expected = combined_text[start:end]
        if item_text != expected:
            first_diff = next(
                (i for i, (a, b) in enumerate(zip(item_text, expected)) if a != b),
                min(len(item_text), len(expected)),
            )
            logger.error(
                "DOM anchor mismatch for spine item %s (href=%s) in '%s': "
                "rebuilt text diverges from extract_text_and_map at local offset %d",
                entry.get("spine_index"), entry.get("href"), filepath, first_diff,
            )
            raise ValueError(
                f"DOM-anchored reconstruction mismatch for spine_index="
                f"{entry.get('spine_index')} href={entry.get('href')!r} in {filepath!r} "
                f"at local offset {first_diff}"
            )

        global_runs = [
            DomRun(
                node_index=r.node_index,
                node_offset_start=r.node_offset_start,
                node_offset_end=r.node_offset_end,
                text=r.text,
                start=start + r.start,
                end=start + r.end,
            )
            for r in runs
        ]
        dom_maps.append(SpineDomMap(
            spine_index=entry["spine_index"],
            href=entry["href"],
            start=start,
            end=end,
            runs=global_runs,
            node_count=node_count,
            text=item_text,
        ))

    return dom_maps


def reconstruct_text(dom_map: List[SpineDomMap]) -> str:
    """Rebuild the full combined text from a DOM anchor map.

    Mirrors ``" ".join(full_text_parts)`` in ``extract_text_and_map`` exactly,
    including the inter-item single-space gap even when an item's own text is
    empty, and using each item's already-validated ``text`` (see
    :class:`SpineDomMap`) rather than re-deriving it from ``runs`` -- so this
    stays correct at inline joins without duplicating :func:`joined_text`'s
    decision. Intended for tests/verification -- the result should be
    byte-identical to ``extract_text_and_map(filepath)[0]`` for the same book.
    """
    return " ".join(item.text for item in dom_map)


def locate_offset(dom_map: List[SpineDomMap], offset: int) -> Optional[Tuple[int, int, int]]:
    """Map a global char offset to ``(spine_index, node_index, node_offset)``.

    ``offset`` is in the same character space as ``extract_text_and_map``'s
    combined text. Returns ``None`` when ``offset`` falls outside every spine
    item's range, or lands in a synthetic separator gap -- the single joining
    space between spine items, or between two runs within an item -- which has no
    corresponding position in the original XHTML.

    :param dom_map: the result of :func:`build_dom_anchor_map`.
    :param offset: a 0-based character offset into the combined text.
    :return: ``(spine_index, node_index, node_offset)`` or ``None``.
    """
    if not dom_map or offset < 0:
        return None

    starts = [item.start for item in dom_map]
    i = bisect_right(starts, offset) - 1
    if i < 0:
        return None
    item = dom_map[i]
    if offset >= item.end:
        return None  # inter-item separator gap (or past the end of the book)

    run_starts = [run.start for run in item.runs]
    j = bisect_right(run_starts, offset) - 1
    if j < 0:
        return None
    run = item.runs[j]
    if offset >= run.end:
        return None  # inter-run separator gap within this item

    node_offset = run.node_offset_start + (offset - run.start)
    return item.spine_index, run.node_index, node_offset
