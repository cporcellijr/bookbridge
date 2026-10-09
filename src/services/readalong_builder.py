"""EPUB 3 read-along assembly: marker injection, SMIL emission, OPF rewriting,
and repackaging.

Builds on Phase 1 (``src/utils/ebook_dom_map.py`` -- DOM anchor map) and Phase 2
(``src/services/readalong_segments.py`` -- sentence/clip table) to produce a new
EPUB whose spine documents carry ``<span id="c<spine>-s<n>">TEXT</span>``
markers wrapping each sentence's own text, with one SMIL media-overlay
document per spine item that has sentences, an OPF rewritten in place to
reference them, and the source audio embedded as-is (no transcode -- that is
Phase 4).

**Anchor strategy: wrap the sentence's own text, not an empty marker span**
(supersedes an earlier design). The original design used an *empty* marker
(``<span id="s42"/>``) at each sentence's start on the reasoning that SMIL
``<par>`` seeking only needs a jump target. That reasoning covered seeking but
never highlighting: a reader resolving ``<text src="...#id"/>`` to an empty
element has no text to highlight. Verified on a real generated book: all
9,034 narration targets in it were empty spans, and highlighting was
confirmed dead in two different EPUB 3 readers. Since the SMIL fragment must
resolve to an element that *contains* the sentence's text, the marker span
now wraps that text instead of merely preceding it.

**The hard case a range-wrap must handle is a sentence crossing an inline
element** (``<em>``, ``<a>``, ...): the sentence's characters then live in
more than one DOM text node, and no single new element can wrap all of them
without splitting the inline element itself. Storyteller solves this by
pre-splitting spine XHTML into ``*_split_NNN.xhtml`` fragments before
wrapping; this module does not need that, because Phase 1's
``ebook_dom_map`` already gives exact node-level provenance
(:func:`~src.utils.ebook_dom_map.locate_offset`, ``DomRun.node_index``/
``node_offset_start``/``node_offset_end``) for every character, so wrapping
can be done **in place**, one existing text node at a time, with no file
splitting. Measured across the local library's real EPUBs with a
``ctc``/``lexical`` alignment map: a sentence crossing at least one inline
element boundary is not rare enough to treat as a corner case.

The chosen behaviour, applied per sentence: locate the DOM run containing the
sentence's own ``char_start`` (the same run the superseded design already
anchored its point-marker to), and wrap that run's text from the sentence's
start up to whichever comes first -- that run's own end, or the sentence's
own ``char_end``. When the sentence's end is reached first, the whole
sentence is wrapped in one element (the common case: a ``<p>`` with no inline
markup). When the run's end is reached first, the sentence continues into
further inline content this wrap does not cover; the marker still resolves
to a real, non-empty element carrying the sentence's *opening* text -- a
**deliberately partial highlight**, not a silent empty one -- and the
occurrence is counted (``ReadalongBuildResult.sentences_crossing_inline_elements``)
rather than hidden. Nothing is split, no inline element is touched, and no
second wrap chases the remainder into the next run: extending across an
inline-element boundary would itself require restructuring that element's
own children, trading a bounded, well-understood highlight gap for the same
"easy to get subtly wrong, easy to emit invalid markup from" risk the
original decision was trying to avoid in the first place -- just relocated
rather than removed. This is a deliberate, now-corrected decision, not
something to silently revisit again without the same kind of on-device
verification that overturned the first one.

This module reuses two Phase 1 internals directly rather than re-implementing
them: ``ebook_dom_map.locate_offset`` (turns a sentence's global char offset
into ``(spine_index, node_index, node_offset)``) and
``ebook_dom_map.content_string_nodes`` (the exact node list bs4's
``get_text()`` would enumerate, in the same order ``locate_offset``'s
``node_index`` indexes into). Re-deriving that filtering here risked drifting
from Phase 1's own carefully-pinned bs4 behaviour notes; importing it keeps
the two modules' idea of "node N" identical by construction. Neither Phase 1
nor Phase 2 is modified by this module.

**SMIL/OPF conventions were read from Storyteller's real writer**
(``storyteller-platform/storyteller``, MIT), specifically
``applications/web/src/assets/library/scanner`` and
``applications/web/src/app/api/v2/books/[bookId]/debug/media-overlay/route.ts``
by way of ``libraries/align/src/align/ctc/mediaOverlay.ts``'s
``createMediaOverlay``: the ``<seq epub:textref="...">``/``<par><text
src="...#id"/><audio src="..." clipBegin="Ns" clipEnd="Ns"/></par>`` shape, and
the plain-seconds-plus-``s`` clock format Storyteller writes
(``${value.toFixed(3)}s``). That format is also already what this repo's own
``src/utils/smil_extractor.py`` (``_parse_timestamp``) expects to read, so it
was independently corroborated, not taken on faith. None of that TypeScript
was copied, ported, or transliterated -- the shape is the public IDPF EPUB 3
Media Overlays specification's own worked examples, and the Python below
(marker grouping/splitting, relative-href computation via ``posixpath.relpath``
against whatever directory layout a given EPUB actually uses, OPF surgery via
lxml that preserves everything not explicitly changed, zip repackaging) is
original, written against this codebase's own data shapes. Storyteller's own
directory layout convention (fixed sibling ``Text/``/``Audio/`` folders, hrefs
hard-coded as ``../Audio/<file>``) is *not* reused, since an arbitrary library
EPUB cannot be assumed to share it; hrefs here are computed with
``posixpath.relpath`` from whatever directories the source EPUB and the
generated ``readalong/`` folder actually land in. Per the same judgment Phase
2 made for its own Storyteller-confirmed-but-not-copied ``-sN`` id shape, no
MIT notice is added to this file.

**Phase 4 Part A -- contiguous clips.** A live run measured the embedded
overlay's summed duration at 4.28% short of the real audio (against
BookOrbit's own ``min(300s, 5%)`` tolerance) -- inter-sentence pauses belong
to no clip, so they are never counted. Storyteller's own SMIL has zero gaps
across all 10,312 ``<par>``s of a real 10.1h book: every clip's ``clipEnd``
equals the next one's ``clipBegin``. This module now reproduces that,
extending each clip's end to the next one's start (:func:`_extend_clips_to_contiguous`)
rather than doing it in ``readalong_segments.py``: contiguity is a property
of the *emitted SMIL sequence* (which sentences actually got a ``<par>``,
after this module's own no-DOM-location drops -- Phase 2 knows nothing about
those), and it needs the real, final embedded audio's probed duration to
extend the book's last clip, which only this module (the one doing the
transcode below) has. Leaving ``SentenceClip.ts_end`` itself untouched in
Phase 2 also keeps it meaning "this sentence's own measured end" for the
per-sentence highlight-range upgrade this module's docstring already floats
as a later step -- extending it there would quietly repurpose it into
"how long to keep highlighting", a different value.

**Phase 4 Part B -- audio packaging.** ``READALONG_AUDIO_BITRATE=source``
copies compatible single-file AAC without re-encoding. Numeric bitrates
transcode to mono AAC; source mode uses 64k when transcoding is necessary.
Multi-file audiobooks are concatenated into one continuous audio timeline.
Concatenation uses ffmpeg's ``concat`` *filter* (full decode of every part,
then concatenate the decoded samples) rather than the ``concat`` demuxer,
because that is exactly what ``ForcedAligner._load_audio`` already does to
build the single timeline the stored alignment map's timestamps are
absolute against -- reproducing that decode order means the timestamps need
no adjustment for whatever this module embeds.

**Phase 4 Part C -- audio file splitting.** Embedding the whole transcoded
audiobook as one physical file measured at 157.2MB for an 11.5h book;
BookOrbit's web reader loads the whole file referenced by a ``<par>`` into
memory and re-loads it on every chapter's overlay, and a blob that size
stops playback. Storyteller ships 8 files (~17.1MB each) for a comparable
10.1h book. That 8-file split is Storyteller's own *source* file layout --
its alignment operates directly against however many original audio parts
the publisher shipped (each ``SentenceRange``/``WordRange`` in
``libraries/align/src/align/ctc/mediaOverlay.ts`` carries its own
``audiofile``, and Storyteller never concatenates them) -- so it is not a
scheme this module can reuse directly: unlike Storyteller, this repo's own
``ForcedAligner._load_audio`` already fully decodes and concatenates every
source part into ONE continuous timeline before alignment ever runs
(Phase 4 Part B's own concat-filter step reproduces exactly that), so every
stored clip timestamp is absolute against that single merged timeline, not
against whichever original file it came from.

This module therefore imposes its own split, independent of the source
file layout: after transcoding (Part B, unchanged) and after
:func:`_extend_clips_to_contiguous` (unchanged -- see that function's own
docstring), the single transcoded file is cut into several physical files
via ``ffmpeg`` stream copy (:func:`_split_audio_into_files`, no re-encode --
the audio is already final), at cut points chosen to fall between clips,
never inside one, and -- wherever the book's own chapter lengths allow it
-- never inside a spine item's whole narration span either
(:func:`_compute_audio_file_boundaries`). Each clip's ``<par>`` then
references whichever physical file its time range landed in, with
``clipBegin``/``clipEnd`` recomputed relative to that file's own start
rather than the whole book (:class:`_PlacedClip`) -- so **contiguity is
established once, globally, exactly as before, and is preserved per file
purely because a cut point is never chosen inside a clip**: every clip
still touches its neighbour's edge, just measured against a shorter, local
timeline when a cut falls between them. Target file size
(``_TARGET_AUDIO_FILE_BYTES``) is converted to a target **duration** from
whatever ``READALONG_AUDIO_BITRATE`` is actually configured
(:func:`_target_audio_file_seconds`) so the file count scales with the
admin's own bitrate choice instead of a fixed duration producing wildly
different sizes at a different bitrate.

**Why cut points prefer chapter boundaries.** A SMIL document referencing
more than one physical audio file is legal, and is what Storyteller's own
artifacts do (its Anansi Boys has 7 of 8 overlays straddling a file
boundary); foliate-js -- the reader BookOrbit serves -- groups a SMIL's
``<par>``s into consecutive per-``src`` runs specifically to support it. So
a straddle is not a correctness problem. It is a *playback quality* one:
foliate loads a whole audio file (``await this.book.loadBlob(src)``, one
HTTP GET per file through BookOrbit's streaming loader) before it can play
the first clip out of it, so a file change in the middle of a chapter buys
a silent stall mid-chapter, where the same change at a chapter boundary
costs nothing the reader was not already paying for a section change. Cut
points therefore prefer whole spine items and fall back to the clip-level
rule only inside a chapter longer than one whole target file, where no cut
point can avoid a straddle anyway. This is a preference, not a floor or a
ceiling on file count: several short chapters still pack into one file, and
a book that is one enormous chapter still splits.
"""
import bisect
import copy
import html
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union, TYPE_CHECKING
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup, NavigableString, PageElement, Tag
from lxml import etree

from src.utils.ebook_dom_map import (
    DomRun,
    SpineDomMap,
    content_string_nodes,
    build_dom_anchor_map,
    joined_text,
    locate_offset,
    original_body_scope,
    parse_original_spine_xml,
    runs_from_nodes,
)
from src.services.epub3_upgrade import _find_opf_path, upgrade_epub2_to_epub3
from src.services.readalong_segments import SentenceClip, build_sentence_clips
from src.utils.forced_aligner import container_timeline_filter

if TYPE_CHECKING:
    from src.services.alignment_service import AlignmentService
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

_OPF_NS = "http://www.idpf.org/2007/opf"
_SMIL_NS = "http://www.w3.org/ns/SMIL"
_OPS_NS = "http://www.idpf.org/2007/ops"

_AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".wav": "audio/x-wav",
}

# Base name for the folder new read-along files (SMIL + embedded audio) are
# written into, sibling to the OPF. Suffixed with a counter on the vanishingly
# rare chance a library EPUB already has an entry with this name.
_READALONG_DIR_BASE = "readalong"

# Declared as the OPF's `media:active-class`. foliate-js (BookOrbit's web
# reader) adds `book.media.activeClass` to the playing sentence verbatim, so an
# undeclared class becomes the literal class "undefined" and nothing
# highlights, while BookOrbit's injected highlight CSS falls back to this exact
# name. It is also the EPUB 3 conventional name, and what Storyteller declares.
_MEDIA_OVERLAY_ACTIVE_CLASS = "-epub-media-overlay-active"

# The LAST clip in each audio file, when it ends at (or within this window of)
# the file's real end, is pushed PAST the end by the overshoot below, so the
# file hands over to the
# next one only through the audio's `ended` event. foliate-js (BookOrbit's web
# reader) advances to the next file from BOTH its `timeupdate` handler (once
# playback passes the run's last clipEnd) and its `ended` handler. At end of
# media the browser fires `timeupdate` while `paused` is still false and then
# `ended`, so a clipEnd even a few ms short of the file's end triggers both:
# two players start the next file and the narration doubles, again at every
# file after. The split files run ~20ms longer than their cut points (AAC
# frame padding), which is exactly how the bridge's clips landed short.
# Storyteller's clips end 10-25ms past their files' ends. The window exceeds
# the 250ms maximum `timeupdate` interval; the overshoot exceeds one AAC frame
# at the encoder's sample rates.
_FILE_END_CLIP_WINDOW_SECONDS = 0.5
_FILE_END_CLIP_OVERSHOOT_SECONDS = 0.1

# bs4's own ``BeautifulSoup.ASCII_SPACES`` (space, LF, tab, form-feed, CR) --
# verified against the installed bs4, not assumed. See
# :func:`_verify_marker_injection` for why its comparison collapses runs of
# these characters on both sides before comparing.
_ASCII_WHITESPACE_RUN_RE = re.compile("[ \t\n\r\f]+")

# ``(start_node_index, start_offset, end_node_index, end_offset_exclusive,
# marker_id)`` -- the DOM range one sentence's marker span must wrap.
_Marker = Tuple[int, int, int, int, str]

# Elements a sentence's marker wrap must never split or cross: the sentence
# splitter already hard-breaks at these, so meeting one inside a sentence's
# range means the range is not a plain inline run.
_BLOCK_LEVEL_TAGS = frozenset({
    "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "section", "article", "aside", "header", "footer", "nav", "figure",
    "figcaption", "table", "tr", "td", "th", "thead", "tbody", "tfoot",
    "ul", "ol", "dl", "dt", "dd", "pre", "body", "html",
})

# --- Stage progress reporting ------------------------------------------------
#
# A live run on an 11.5h audiobook sat on "parsing" for 5m29s with zero
# intermediate signal -- the whole build/deliver pipeline is a single
# terminal Job update (progress=0.0 at queue time, 1.0/last_error at the
# end), so every real stage in between looked indistinguishable from a hang.
# `ReadalongProgressCallback` is threaded from the web worker
# (`web_server._readalong_epub_worker`) down through `deliver_readalong_epub`
# and `build_readalong_epub` so each stage transition can be persisted (see
# `Job.stage`/`Job.progress`) as it happens, not just at the very end.
#
# `_STAGE_START` is the overall-progress fraction each stage begins at, in
# call order -- approximate, not measured per book, but weighted so the one
# genuinely expensive step (transcoding the whole book's audio through
# ffmpeg) gets the lion's share of the range instead of a same-size slice as
# the cheap steps around it.
ReadalongProgressCallback = Callable[[str, float], None]

_STAGE_START: Dict[str, float] = {
    "resolving_audio": 0.00,
    "converting_epub": 0.05,
    "parsing_epub": 0.10,
    "transcoding_audio": 0.20,
    "building_overlays": 0.85,
    "packaging": 0.93,
    "delivering": 0.97,
}


def _safe_progress(
    progress_callback: Optional[ReadalongProgressCallback], stage: str, fraction: float,
) -> None:
    """Report ``(stage, fraction)`` to ``progress_callback``, never letting a
    failure abort generation.

    Progress reporting is a best-effort UI nicety layered on top of a real
    generation pipeline -- a broken callback (e.g. a failed DB write in the
    caller) must never be the reason a book fails to generate. Any exception
    it raises is logged and swallowed here rather than propagated.
    """
    if progress_callback is None:
        return
    try:
        progress_callback(stage, max(0.0, min(1.0, fraction)))
    except Exception as e:
        logger.warning(
            "Read-along progress callback failed at stage '%s' (%.2f): %s",
            stage, fraction, e, exc_info=True,
        )


@dataclass(frozen=True)
class SpineOverlayResult:
    """One spine item's generated media overlay.

    ``href`` is the spine item's full archive path (matches
    ``extract_text_and_map``'s ``spine_map`` entry exactly). ``smil_href`` is
    the generated SMIL document's own full archive path. ``duration_seconds``
    is the sum of its emitted clips' durations -- the per-overlay
    ``media:duration`` the OPF records for it.
    """
    spine_index: int
    href: str
    smil_href: str
    par_count: int
    duration_seconds: float


@dataclass(frozen=True)
class ReadalongBuildResult:
    """The full output of :func:`build_readalong_epub`.

    ``dropped_no_timestamp`` is carried over from Phase 2's
    ``SentenceClipResult`` (a sentence whose alignment map lookup returned no
    timestamp for a boundary). ``dropped_no_location`` counts sentences Phase 2
    did produce a clip for, but whose start offset this phase could not place
    in the DOM (``ebook_dom_map.locate_offset`` returned ``None``, or
    disagreed about which spine item it belongs to) -- both are excluded from
    the generated SMIL, since a ``<text>`` reference to a fragment id that was
    never inserted collapses playback to the chapter start.
    ``dropped_spine_items_injection_failed`` counts whole spine items (not
    individual sentences) excluded because marker injection could not be
    verified on either the fidelity or the reconstructed-content path -- a
    pre-existing, independent defect where certain non-ASCII whitespace at a
    marker's split point is silently altered by bs4's own per-node
    ``get_text(strip=True)``; the affected spine item is still carried
    through byte-for-byte, just with no read-along overlay for its sentences.

    ``total_duration_seconds`` (Phase 4 Part A) is the summed *contiguous*
    overlay duration -- every clip's end already reaches the next one's start
    (or, for the book's very last clip, the real embedded audio's own probed
    length), so this should land close to the full audio duration rather than
    running short by the sum of every inter-sentence pause. ``audio_bitrate``
    is the configured numeric bitrate (or the safe default for an invalid
    setting). In source mode it is the prepared file's measured average
    bits per second, including container overhead used to size chunks.

    ``sentences_crossing_inline_elements`` counts sentences whose own text
    spans more than one DOM node (crosses an ``<em>``/``<a>``/... boundary),
    so their marker span could only wrap the first run's portion -- a
    deliberate partial highlight, not a dropped sentence or an empty target
    (see the module docstring's "Anchor strategy" section). These sentences
    still get a full, correctly-timed SMIL ``<par>``; only their highlighted
    text is incomplete.

    ``unnarrated_spine_items_skipped`` / ``unnarrated_sentences_skipped``
    count the spine items (and their sentences) left without an overlay
    because their narration rate exceeded
    ``_MAX_SECTION_NARRATION_CHARS_PER_SECOND`` -- unread front/back matter
    the aligner squeezed into a moment. ``sentences_interpolated`` is the
    number of emitted sentences with no aligned word inside them (CTC maps
    only; ``None`` for other alignment methods, whose sparse anchors make
    the figure meaningless). ``zero_length_clips`` counts placed SMIL clips
    shorter than 0.05s.

    ``audio_hrefs`` (Phase 4 Part C) lists every embedded physical audio
    file this build produced, as OPF-manifest-relative hrefs, in file order
    -- one entry when the book was short enough to need no split (the
    common case for most of the library), several for a long book (see the
    module docstring's "Phase 4 Part C" section). Every SMIL ``<par>``'s own
    ``<audio src>`` resolves to exactly one of these.
    """
    abs_id: str
    output_path: str
    spine_overlays: List[SpineOverlayResult]
    total_sentences: int
    dropped_no_timestamp: int
    dropped_no_location: int
    total_duration_seconds: float
    audio_hrefs: List[str]
    audio_bitrate: str
    dropped_spine_items_injection_failed: int = 0
    sentences_crossing_inline_elements: int = 0
    unnarrated_spine_items_skipped: int = 0
    unnarrated_sentences_skipped: int = 0
    sentences_interpolated: Optional[int] = None
    zero_length_clips: int = 0


def _opf_package_version(opf_bytes: bytes) -> Optional[str]:
    """The OPF ``<package version="...">`` attribute, or ``None`` if the OPF
    fails to parse or the attribute is absent.

    ``build_readalong_epub`` refuses anything that does not start with
    ``"3"`` rather than emitting a package that claims to be EPUB 2 while
    carrying EPUB-3-only media overlays.
    """
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        tree = etree.fromstring(opf_bytes, parser=parser)
    except etree.XMLSyntaxError as e:
        logger.warning("Could not parse OPF to read package version: %s", e, exc_info=True)
        return None
    return tree.get("version")


def _opf_has_media_overlays(opf_bytes: bytes) -> bool:
    """Whether the OPF already declares EPUB 3 media overlays.

    Detected by either a manifest ``<item media-overlay="...">`` attribute
    (a spine document already wired to a SMIL) or a publication-level
    ``<meta property="media:duration">`` with no ``refines`` (the one global
    duration EPUB 3 permits -- https://www.w3.org/TR/epub-33/#sec-duration).
    ``_rewrite_opf`` appends its own global ``media:duration``, so a source
    that already carries one is refused. Returns ``False`` if the OPF fails
    to parse here
    -- the existing, later parse in the caller is what actually surfaces a
    parse failure.
    """
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        tree = etree.fromstring(opf_bytes, parser=parser)
    except etree.XMLSyntaxError as e:
        logger.warning(
            "Could not parse OPF to check for existing media overlays: %s",
            e, exc_info=True,
        )
        return False
    manifest = tree.find(f"{{{_OPF_NS}}}manifest")
    if manifest is not None:
        for item in manifest.findall(f"{{{_OPF_NS}}}item"):
            if item.get("media-overlay"):
                return True
    metadata = tree.find(f"{{{_OPF_NS}}}metadata")
    if metadata is not None:
        for meta in metadata.findall(f"{{{_OPF_NS}}}meta"):
            if meta.get("property") == "media:duration" and not meta.get("refines"):
                return True
    return False


def _unique_archive_dir(zip_names: set, opf_dir: str, base: str) -> str:
    """A directory (relative to ``opf_dir``) that collides with no existing entry."""
    candidate_base = posixpath.join(opf_dir, base) if opf_dir else base
    candidate = candidate_base
    n = 2
    prefix = candidate + "/"
    while any(name.startswith(prefix) or name == candidate for name in zip_names):
        candidate = f"{candidate_base}-{n}"
        prefix = candidate + "/"
        n += 1
    return candidate


def _unique_manifest_id(existing_ids: set, base: str) -> str:
    """A manifest ``id`` that collides with no id already in ``existing_ids``."""
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def _encode_href_path(path: str) -> str:
    """Percent-encode a decoded, filesystem-style relative path into a valid
    URI reference for use as an OPF/SMIL ``href``/``src`` attribute value.

    Generated references are built from decoded archive paths, so each path
    segment is percent-encoded before it is written into XML. ``/`` remains
    the path separator.
    """
    return quote(path, safe="/")


def _audio_media_type(path: Union[str, Path]) -> str:
    """Best-effort ``media-type`` for the embedded audio file's extension."""
    ext = Path(path).suffix.lower()
    guessed = _AUDIO_MEDIA_TYPES.get(ext)
    if guessed:
        return guessed
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


# ffmpeg accepts a bare bit-rate number or one suffixed with k/K (kilobits) or
# m/M (megabits) for -b:a (e.g. "32k", "128000", "1.5M"). Anything else is
# rejected by _resolve_audio_bitrate rather than handed to the subprocess.
_BITRATE_RE = re.compile(r'^\d+(\.\d+)?[kKmM]?$')

# Keep the existing storage-saving default; source mode avoids re-encoding
# compatible AAC and uses a higher bitrate when transcoding is necessary.
_DEFAULT_AUDIO_BITRATE = "32k"
_SOURCE_FALLBACK_AUDIO_BITRATE = "64k"


def _resolve_audio_bitrate() -> str:
    """Read ``READALONG_AUDIO_BITRATE`` per call -- never cached at import or
    in a Singleton's ``__init__`` (CLAUDE.md's settings-system rule: the
    Settings UI writes ``os.environ`` immediately and every consumer must see
    it without a restart).

    Falls back to :data:`_DEFAULT_AUDIO_BITRATE` -- logging a warning, never
    raising -- for anything other than ``source`` or an ffmpeg bitrate.
    An admin typo in this setting must degrade generation to a safe default,
    not abort it.
    """
    raw = os.environ.get("READALONG_AUDIO_BITRATE", _DEFAULT_AUDIO_BITRATE).strip()
    if raw.lower() == "source":
        return "source"
    if not _BITRATE_RE.match(raw):
        logger.warning(
            "⚠️ READALONG_AUDIO_BITRATE=%s is not a valid ffmpeg bitrate "
            "(expected 'source' or e.g. '32k'); using default %s",
            raw, _DEFAULT_AUDIO_BITRATE,
        )
        return _DEFAULT_AUDIO_BITRATE
    return raw


# --- Phase 4 Part C: audio file splitting -----------------------------------
#
# Target physical size of ONE embedded audio file. This size IS the start-up
# latency at every file transition in BookOrbit's reader: foliate-js's
# MediaOverlay fetches a whole audio file over HTTP and wraps it in a blob
# URL -- URL.createObjectURL(await this.book.loadBlob(src)) -- before the
# first clip of that file can play. Its #play() also leaves this.#audio unset
# across that await, so a second #play()/start() arriving during the fetch
# strands an untracked <audio> element that still autoplays on
# canplaythrough and can never be paused again -- the reported "plays twice
# simultaneously". Both costs scale with this number.
#
# Measured against Storyteller's own artifact for the same book rather than
# reasoned about from first principles: Storyteller's Ghost Academy ships 40
# files at a ~4.5MB / ~18.7min median, ours shipped 11 at ~15.2MB / ~62.8min.
#
# Converted into a target DURATION per file from whatever bitrate is actually
# configured (_resolve_audio_bitrate, read per call) so the file count scales
# with the admin's own bitrate choice rather than a fixed duration producing
# very differently-sized files at a different bitrate.
#
# 1.5MB, not Storyteller's ~4.5MB: BookOrbit resumes narration at a saved
# sentence with start(section, filter) and, if nothing has highlighted 700ms
# later, calls start(section) again -- while the first start is still fetching
# its audio file, so both play (the stranded-<audio> case above). Measured
# through a Cloudflare-proxied BookOrbit, which never caches .m4a: 6.37MB took
# 744-883ms and doubled on every web resume; Storyteller's 5.86MB .mp4 came
# from Cloudflare's cache in ~470ms. Uncached throughput was ~8MB/s, so a
# ~1.5MB file (a long section's pieces top out near 1.5x that) starts inside
# the window without relying on any cache.
_TARGET_AUDIO_FILE_BYTES = int(1.5 * 1024 * 1024)  # ~1.5MB

# Bounds on the DERIVED target duration itself, so an unusually low or high
# configured bitrate can't drive the file count to an absurd extreme (a
# very low bitrate would otherwise stretch the target duration -- and so
# each file's length -- without bound; a very high one would shrink it well
# below any duration worth a separate file).
_MIN_AUDIO_FILE_SECONDS = 180.0    # 3 minutes
_MAX_AUDIO_FILE_SECONDS = 7200.0   # 2 hours

# A spine item with less narrated audio than this gets no media overlay: every
# overlaid section needs a physical file of its own (see
# _compute_audio_file_boundaries), and a file this short is a stream-copy cut
# of one or two AAC frames that may not probe at all. These are front-matter
# pages the alignment squeezed into a few milliseconds -- Good Intentions'
# title and copyright pages got 0.006-0.899s -- and Storyteller leaves the same
# pages without narration. Their audio is absorbed by the preceding clip.
_MIN_SECTION_NARRATION_SECONDS = 1.0

# A spine item narrated faster than this gets no media overlay either: it is
# text the narrator never reads. Measured on 10 real read-alongs, genuine
# chapters run 11.9-18.0 characters/second, while unnarrated front/back matter
# (copyright notices, newsletter sign-ups, tables of contents, acknowledgments,
# about-the-author) that the aligner squeezed into a second or two runs
# 33.7-409 chars/s, so the highlight would race through dozens of sentences.
# (The CTC aligner forces unspoken text through at least one 20 ms frame per
# character, ~50 chars/s.) 28 sits well above the fastest real chapter and
# below the slowest squeezed page. Like the minimum above, the audio is
# absorbed by the preceding clip.
_MAX_SECTION_NARRATION_CHARS_PER_SECOND = 28.0

# A placed SMIL clip shorter than this is reported as zero-length in the
# per-build quality summary: too brief to highlight visibly.
_ZERO_LENGTH_CLIP_SECONDS = 0.05

# Unit suffixes ffmpeg's -b:a accepts (see _BITRATE_RE), mapped to their
# multiplier against bits/second.
_BITRATE_UNIT_BPS = {"": 1, "k": 1_000, "m": 1_000_000}


def _bitrate_to_bps(bitrate: str) -> Optional[int]:
    """Parse an ffmpeg ``-b:a`` value (``"32k"``, ``"128000"``, ``"1.5M"``)
    to bits per second, or ``None`` if it does not match the shape
    :data:`_BITRATE_RE` (and therefore :func:`_resolve_audio_bitrate`)
    already requires.
    """
    stripped = bitrate.strip()
    if not _BITRATE_RE.match(stripped):
        return None
    unit = ""
    numeric = stripped
    if numeric[-1] in "kKmM":
        unit = numeric[-1].lower()
        numeric = numeric[:-1]
    try:
        return int(float(numeric) * _BITRATE_UNIT_BPS[unit])
    except ValueError:
        return None


def _target_audio_file_seconds(bitrate: str) -> float:
    """The target duration for one embedded audio file at ``bitrate``, sized
    to land around :data:`_TARGET_AUDIO_FILE_BYTES`, clamped to
    :data:`_MIN_AUDIO_FILE_SECONDS`/:data:`_MAX_AUDIO_FILE_SECONDS` regardless
    of how far the configured bitrate would otherwise skew the arithmetic.

    Falls back to :data:`_DEFAULT_AUDIO_BITRATE`'s own bits-per-second value
    -- never raises -- if ``bitrate`` itself fails to parse (should not
    happen in practice, since callers pass :func:`_resolve_audio_bitrate`'s
    own already-validated return value, but this function does not assume
    that).
    """
    bps = _bitrate_to_bps(bitrate) or _bitrate_to_bps(_DEFAULT_AUDIO_BITRATE)
    target = (_TARGET_AUDIO_FILE_BYTES * 8) / bps
    return max(_MIN_AUDIO_FILE_SECONDS, min(_MAX_AUDIO_FILE_SECONDS, target))


def _compute_audio_file_boundaries(
    clips: List[SentenceClip], audio_duration_seconds: float, target_seconds: float,
) -> List[Tuple[float, float]]:
    """Partition ``[0, audio_duration_seconds)`` into contiguous ``(start,
    end)`` physical-file ranges, one or more per run of a spine item's audio,
    never one shared by two spine items.

    Every change of spine item along the audio timeline is a cut, placed where
    the earlier item's last clip ends, so that clip ends its file and foliate's
    MediaOverlay moves to the next section on that file's ``ended`` event. iOS
    WebKit lets a script start audio without a tap only just after another
    audio finished playing, and foliate starts a new ``<audio>`` for every
    section: a section change in the middle of a file (``timeupdate``, then
    ``pause``) is refused with NotAllowedError and narration stops at every
    chapter. Storyteller's artifacts cut at every section -- all 20 of Good
    Intentions' section changes follow a file end, against 22 of 26 of ours
    before this rule (88 of 109 on The Employees).

    A run longer than ``target_seconds`` is split into ``round(length /
    target_seconds)`` roughly equal files, each cut nudged forward to the end
    of the clip it would land inside, so no cut falls inside a clip and every
    file still ends on a clip's end.

    ``clips`` must be every clip that will get a SMIL ``<par>`` (after
    :func:`_extend_clips_to_contiguous`), in any order: out-of-order narration
    (issue #426) hands them over in reading order, so they are sorted by
    ``ts_start`` here. An interleaved spine item simply gets several runs.
    """
    if audio_duration_seconds <= 0:
        return [(0.0, max(0.0, audio_duration_seconds))]

    runs: List[List[SentenceClip]] = []
    for clip in sorted(clips, key=lambda c: c.ts_start):
        if runs and runs[-1][-1].spine_index == clip.spine_index:
            runs[-1].append(clip)
        else:
            runs.append([clip])

    cuts: List[float] = []

    def add_cut(candidate: float) -> None:
        # A cut at or before the previous one, or at the audio's own end,
        # would create an empty file.
        if (cuts and candidate <= cuts[-1]) or candidate <= 0.0 or candidate >= audio_duration_seconds:
            return
        cuts.append(candidate)

    for index, run in enumerate(runs):
        run_start = cuts[-1] if cuts else 0.0
        if index == len(runs) - 1:
            run_end = audio_duration_seconds
        else:
            run_end = min(run[-1].ts_end, runs[index + 1][0].ts_start)
        pieces = max(1, round((run_end - run_start) / target_seconds)) if target_seconds > 0 else 1
        step = (run_end - run_start) / pieces
        starts = [c.ts_start for c in run]
        for k in range(1, pieces):
            candidate = run_start + step * k
            enclosing = bisect.bisect_right(starts, candidate) - 1
            if 0 <= enclosing < len(run) and run[enclosing].ts_start < candidate < run[enclosing].ts_end:
                candidate = run[enclosing].ts_end
            if candidate < run_end:
                add_cut(candidate)
        if index < len(runs) - 1:
            add_cut(run_end)

    boundaries: List[Tuple[float, float]] = []
    start = 0.0
    for cut in cuts:
        boundaries.append((start, cut))
        start = cut
    boundaries.append((start, audio_duration_seconds))
    return boundaries



def _split_audio_into_files(
    source_path: Path, boundaries: List[Tuple[float, float]], output_dir: Path,
) -> Optional[List[Tuple[Path, float]]]:
    """Split ``source_path`` (the single, already-transcoded embed audio)
    into one physical file per ``boundaries`` range via ``ffmpeg`` stream
    copy (no re-encode -- the audio is already final).

    Returns a list of ``(file_path, real_probed_duration)`` in the same
    order as ``boundaries`` -- the caller is expected to clamp each file's
    own clips' relative end times to that file's *real* probed duration
    rather than trust the arithmetic ``end - start`` difference, since
    stream-copying a compressed codec (AAC/M4A here) can only cut on the
    codec's own frame grid (~21-23ms for a typical 44.1/48kHz AAC stream) --
    immaterial next to sentence-length clips, but real.

    Returns ``None`` -- never raises -- on any ffmpeg/ffprobe failure, so
    the caller can refuse the build the same way every other guard in this
    module does.
    """
    ext = source_path.suffix
    results: List[Tuple[Path, float]] = []
    for index, (start, end) in enumerate(boundaries):
        out_path = output_dir / f"audio-{index}{ext}"
        cmd = [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(source_path),
            "-t", f"{max(0.0, end - start):.3f}",
            "-c", "copy", str(out_path),
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error(
                "Read-along audio split failed for part %d/%d (%.3f-%.3fs): %s",
                index + 1, len(boundaries), start, end, e, exc_info=True,
            )
            return None
        duration = _probe_duration_seconds(out_path)
        if duration is None:
            logger.error(
                "Read-along audio split: could not probe duration of part %d/%d ('%s')",
                index + 1, len(boundaries), out_path,
            )
            return None
        results.append((out_path, duration))
    return results


def _normalize_audio_paths(
    audio_paths: Union[str, Path, Sequence[Union[str, Path]]],
) -> List[Path]:
    """Normalize the caller's audio input to an ordered list of ``Path``s.

    A bare ``str``/``Path`` (the common single-file case) becomes a
    one-element list rather than being iterated character-by-character.
    Order is preserved exactly as given and never re-sorted: for a
    multi-file audiobook this must already be the same order the book was
    force-aligned against (``ForcedAligner._load_audio`` decodes parts in
    the order it is given them, back to back, with nothing trimmed or added
    between them -- see this module's docstring), since that order is what
    the stored alignment map's timestamps are absolute against.
    """
    if isinstance(audio_paths, (str, Path)):
        return [Path(audio_paths)]
    return [Path(p) for p in audio_paths]


def _run_ffmpeg_with_progress(
    cmd: List[str], total_duration: float, progress_callback: Callable[[float], None],
) -> None:
    """Run an ffmpeg command, reporting fractional completion as it decodes.

    Parses ffmpeg's own machine-readable ``-progress pipe:1`` stream, keying
    off ``out_time=<H:MM:SS.ffffff>`` rather than the also-emitted
    ``out_time_ms`` field -- the latter is, despite its name, actually
    microseconds (a long-standing ffmpeg quirk kept for backward
    compatibility), which is easy to get wrong; the formatted clock string
    has no such ambiguity. stderr is merged into the same stream
    (``STDOUT``) so a single blocking read loop can never deadlock on an
    unread pipe filling up -- non-progress lines (real error output, since
    ``-loglevel error`` keeps this otherwise near-silent) are simply not
    ``out_time=``-prefixed and are collected as a short tail for the
    exception raised on failure instead.

    Raises ``subprocess.CalledProcessError`` on a non-zero exit -- callers
    already catch that alongside ``FileNotFoundError`` for the no-progress
    path. ``progress_callback`` is expected to already be exception-safe
    (see ``_safe_progress``); it is not wrapped again here.
    """
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    ) as proc:
        tail: List[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time="):
                value = line.split("=", 1)[1].strip()
                try:
                    hours, minutes, seconds = value.split(":")
                    elapsed = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                except (ValueError, IndexError):
                    continue
                if total_duration > 0:
                    progress_callback(min(1.0, max(0.0, elapsed / total_duration)))
            elif line:
                tail.append(line)
                del tail[:-20]
        returncode = proc.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output="\n".join(tail))


def _can_copy_source_aac(path: Path) -> bool:
    """Check the first audio stream for browser-compatible AAC LC."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,profile", "-of", "json", str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
        return bool(
            streams and streams[0].get("codec_name") == "aac"
            and streams[0].get("profile") == "LC"
        )
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        logger.warning("Could not probe source AAC for '%s': %s", path, e, exc_info=True)
        return False


def _transcode_audio_for_embed(
    audio_paths: List[Path], bitrate: str, output_path: Path,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> bool:
    """Prepare one AAC file, copying compatible single-file AAC in source mode.

    Numeric bitrates transcode to mono AAC. Source mode falls back to 64k
    mono AAC for other codecs, multiple inputs, or a failed stream copy.

    Multi-file audiobooks (Grimmory/BookOrbit both stage tracks to disk as
    ``track_000.<ext>``, ``track_001.<ext>``, ... -- see
    ``forge_service.py``'s ``_copy_*_audio_files``) are joined with ffmpeg's
    ``concat`` *filter*, not the ``concat`` *demuxer*: the filter fully
    decodes every input and concatenates the decoded samples, which is
    exactly what ``ForcedAligner._load_audio`` already does (each part
    streamed through its own ffmpeg decode into one continuous buffer, in
    list order) to build the single timeline the stored alignment map's
    timestamps are absolute against. Reproducing that same decode-then-concat
    semantics here means those timestamps need no adjustment for whatever
    actually ends up embedded, whether it is one file or many.

    This is the single most expensive step of the whole read-along pipeline
    (a live 11.5h book: 5m29s of a 5m43s total build). When
    ``progress_callback`` is given, the source parts are pre-probed via
    ``ffprobe`` for their total duration and ffmpeg is run with
    ``-progress`` so the callback is invoked with fractional (0.0-1.0)
    completion as the transcode actually runs, instead of only at the very
    start and end. Falls back to a plain, unmonitored run -- identical to
    the no-callback behaviour -- when no callback is given or the source
    duration cannot be probed.

    Returns ``False`` -- never raises -- on any ffmpeg failure, including
    ffmpeg not being installed, so the caller can refuse the build the same
    way every other guard in this module does.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    copy_source = (
        bitrate == "source" and len(audio_paths) == 1
        and _can_copy_source_aac(audio_paths[0])
    )
    if bitrate == "source" and not copy_source:
        bitrate = _SOURCE_FALLBACK_AUDIO_BITRATE
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
    for path in audio_paths:
        cmd += ["-i", str(path)]
    if len(audio_paths) > 1:
        # Each part decoded exactly as the aligner timed the map against it,
        # then joined.
        parts = "".join(
            f"[{i}:a:0]{container_timeline_filter(path) or 'anull'}[p{i}];"
            for i, path in enumerate(audio_paths)
        )
        joined = "".join(f"[p{i}]" for i in range(len(audio_paths)))
        cmd += [
            "-filter_complex", f"{parts}{joined}concat=n={len(audio_paths)}:v=0:a=1[aout]",
            "-map", "[aout]",
        ]
    else:
        cmd += ["-map", "0:a:0"]
        timeline = None if copy_source else container_timeline_filter(audio_paths[0])
        if timeline:
            cmd += ["-af", timeline]
    cmd += ["-vn", "-sn"]
    if copy_source:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-ac", "1", "-c:a", "aac", "-b:a", bitrate]

    total_duration: Optional[float] = None
    if progress_callback is not None:
        total_duration = 0.0
        for path in audio_paths:
            duration = _probe_duration_seconds(path)
            if duration is None:
                total_duration = None
                break
            total_duration += duration

    if total_duration:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(output_path)]

    try:
        if total_duration and progress_callback is not None:
            _run_ffmpeg_with_progress(cmd, total_duration, progress_callback)
        else:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if copy_source:
            logger.info(
                "Read-along audio preserved source AAC without re-encoding: '%s'", audio_paths[0],
            )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if copy_source:
            logger.warning(
                "Read-along source AAC copy failed; retrying at %s: %s",
                _SOURCE_FALLBACK_AUDIO_BITRATE, e, exc_info=True,
            )
            return _transcode_audio_for_embed(
                audio_paths, _SOURCE_FALLBACK_AUDIO_BITRATE, output_path, progress_callback,
            )
        logger.error(
            "Read-along audio transcode failed for %d part(s) at bitrate %s: %s",
            len(audio_paths), bitrate, e, exc_info=True,
        )
        return False


def _probe_duration_seconds(path: Union[str, Path]) -> Optional[float]:
    """The real duration, in seconds, of an audio file via ``ffprobe``.

    Mirrors ``Transcriber.get_audio_duration``'s own ffprobe invocation
    rather than importing it: that class's module pulls in the
    transcription stack's heavier dependencies for what is, here, a single
    stdlib subprocess call. Returns ``None`` -- never raises -- on any
    failure, so the caller can degrade to leaving the book's final clip
    un-extended instead of crashing the whole build.
    """
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        logger.warning("Could not probe audio duration for '%s': %s", path, e, exc_info=True)
        return None


def _format_smil_clock(seconds: float) -> str:
    """Format seconds as an EPUB 3 ``media:duration`` clock value (``H:MM:SS.mmm``).

    Matches the IDPF Media Overlays 3.0 spec's own worked examples
    (``0:32:29.000``, ``1:57:35.000``) -- hours unpadded, minutes/seconds
    zero-padded to 2 digits, milliseconds to 3.
    """
    total_ms = round(max(0.0, seconds) * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"


# An existing element's `id="..."` attribute value, in any quoting style a
# real-world (possibly not-strictly-XML) spine document might use. Used only
# to collect the set of ids already in use in a spine item's markup -- a
# plain regex scan over the raw content string, not a full parse, so it
# works identically whichever bytes end up being injected into (the
# ORIGINAL archive bytes for Finding 1's fidelity path, or the
# ebooklib-reconstructed `content` for its fallback).
_EXISTING_ID_RE = re.compile(r'\bid\s*=\s*(["\'])(.*?)\1')


def _existing_ids_in_markup(content: Union[str, bytes]) -> set:
    """Every ``id="..."`` value already present anywhere in ``content``,
    resolved to what an XML/HTML parser will actually see.

    Existing ids may contain character references in the raw markup. Decode
    those values before allocating marker ids so generated fragments remain
    unique after the XHTML parser resolves the attributes.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return {html.unescape(match.group(2)) for match in _EXISTING_ID_RE.finditer(content)}


def _allocate_marker_id(desired_id: str, existing_ids: set) -> str:
    """A collision-free id for a sentence marker, reserving it in
    ``existing_ids`` (mutated in place) so a later call in the same spine
    item never re-allocates the same fallback.

    Deterministic: given the same ``desired_id`` and the same starting
    ``existing_ids``, this always returns the same value -- the same
    numeric-suffix scheme :func:`_unique_manifest_id` already uses elsewhere
    in this module. Determinism matters here specifically (Finding 6):
    regenerating the same book must allocate the same ids for the same
    sentences, or a reader's stored position (a sentence id) silently starts
    pointing at the wrong marker after a regeneration.
    """
    if desired_id not in existing_ids:
        existing_ids.add(desired_id)
        return desired_id
    n = 2
    while f"{desired_id}-{n}" in existing_ids:
        n += 1
    allocated = f"{desired_id}-{n}"
    existing_ids.add(allocated)
    return allocated


def _markers_for_spine_item(
    dom_map: List[SpineDomMap],
    clips: List[SentenceClip],
    spine_index: int,
    existing_ids: set,
) -> Tuple[List[_Marker], int, Dict[str, str], int]:
    """Resolve each clip's sentence text to a DOM wrap range.

    Returns ``(markers, dropped, sentence_id_to_marker_id, crossed_inline_boundary)``.
    ``markers`` are ``(start_node_index, start_offset, end_node_index,
    end_offset, marker_id)`` tuples ready for
    :func:`_inject_markers`/:func:`_inject_markers_into_original` --
    ``start_offset`` is the sentence's first character inside its start
    node's own text and ``end_offset`` the exclusive end inside the node
    holding its last real character, so the wrapping ``<span>`` carries the
    whole sentence even when it crosses inline elements (see
    :func:`_splice_markers`).

    The start comes from :func:`locate_offset` on ``char_start``; the end from
    the last run of this spine item that starts before ``char_end`` (clamped
    to ``char_end``), which also covers an end landing in a separator gap.
    ``crossed_inline_boundary`` counts sentences whose end could not be
    resolved at or after their start run and so fall back to wrapping only
    the start run; :func:`_splice_markers` adds the sentences it could not
    fully wrap without splitting a block-level element.

    ``dropped`` counts sentences excluded because ``locate_offset`` returned
    ``None`` (the offset landed in a synthetic separator gap -- should not
    happen for a real sentence start, see this module's docstring, but is not
    assumed) or resolved to a different spine item than expected.
    ``sentence_id_to_marker_id`` maps each placed clip's stable
    ``SentenceClip.sentence_id`` to the id actually allocated for it
    (:func:`_allocate_marker_id`, seeded from ``existing_ids`` -- Finding 6)
    -- almost always identical to ``sentence_id`` itself, differing only when
    a collision was found. The caller must use the allocated value, not
    ``sentence_id``, when building this spine item's SMIL, so the ``<par>``
    id and its ``<text src="...#...">`` fragment agree with whatever id
    actually landed in the XHTML.

    :param existing_ids: every id already used in this spine item's own
        markup (:func:`_existing_ids_in_markup`) -- mutated in place as ids
        are allocated, so this must be a fresh set per spine item, never
        shared across spine items (id uniqueness is a per-document XML
        requirement, not a book-wide one).
    """
    node_index_to_run: Dict[int, DomRun] = {
        run.node_index: run
        for entry in dom_map
        if entry.spine_index == spine_index
        for run in entry.runs
    }

    spine_runs: List[DomRun] = [
        run for entry in dom_map if entry.spine_index == spine_index for run in entry.runs
    ]
    spine_run_starts = [run.start for run in spine_runs]

    markers: List[_Marker] = []
    sentence_id_to_marker_id: Dict[str, str] = {}
    dropped = 0
    crossed_inline_boundary = 0
    for clip in clips:
        located = locate_offset(dom_map, clip.char_start)
        if located is None or located[0] != spine_index:
            logger.warning(
                "Read-along marker: could not place sentence %s (char %d) in "
                "spine item %d; dropping its SMIL par",
                clip.sentence_id, clip.char_start, spine_index,
            )
            dropped += 1
            continue
        _, node_index, node_offset = located
        run = node_index_to_run.get(node_index)
        if run is None:
            logger.error(
                "Read-along marker: locate_offset resolved sentence %s to "
                "node %d, which has no run in spine item %d's DOM map; "
                "dropping its SMIL par",
                clip.sentence_id, node_index, spine_index,
            )
            dropped += 1
            continue

        end_run = run
        end_pos = bisect.bisect_left(spine_run_starts, clip.char_end) - 1
        if end_pos >= 0 and spine_runs[end_pos].node_index >= run.node_index:
            end_run = spine_runs[end_pos]
        wrap_char_end = min(clip.char_end, end_run.end)
        if end_run is run and wrap_char_end < clip.char_end:
            crossed_inline_boundary += 1
            logger.debug(
                "Read-along marker: sentence %s crosses an inline-element "
                "boundary in spine item %d; its highlight target covers only "
                "chars %d-%d of the sentence's full %d-%d",
                clip.sentence_id, spine_index, clip.char_start, wrap_char_end,
                clip.char_start, clip.char_end,
            )
        node_offset_end = end_run.node_offset_start + (wrap_char_end - end_run.start)

        marker_id = _allocate_marker_id(clip.sentence_id, existing_ids)
        if marker_id != clip.sentence_id:
            logger.info(
                "Read-along marker: id '%s' already used elsewhere in spine "
                "item %d's markup; allocated '%s' instead",
                clip.sentence_id, spine_index, marker_id,
            )
        markers.append((node_index, node_offset, end_run.node_index, node_offset_end, marker_id))
        sentence_id_to_marker_id[clip.sentence_id] = marker_id
    return markers, dropped, sentence_id_to_marker_id, crossed_inline_boundary


def _extend_clips_to_contiguous(
    clips: List[SentenceClip], audio_duration_seconds: Optional[float],
) -> List[SentenceClip]:
    """Extend each clip's ``ts_end`` to the next clip's ``ts_start`` (Phase 4
    Part A), so playback highlighting never goes dark during an
    inter-sentence pause and the summed overlay duration matches the real
    audio instead of running short by the sum of every pause -- measured
    4.28% short on a real book before this fix, against BookOrbit's own
    ``min(300s, 5%)`` duration-mismatch tolerance.

    ``clips`` must already be in book reading order (spine order, then each
    spine item's own sentence order) *and* already be the sentences that will
    actually be emitted as SMIL ``<par>``s -- i.e. after
    :func:`_markers_for_spine_item`'s no-DOM-location drops, not the raw
    Phase 2 output. Extending against a sentence that never gets its own
    ``<par>`` would silently absorb its pause into the wrong neighbour.

    ``ts_start`` is never touched; only ``ts_end`` ever grows. Segment metadata
    keeps each extension inside its own narration block: same-segment gaps end
    at the next clip, while a segment's terminal clip ends at that segment's
    own boundary. The terminal clip of the latest narrated segment may reach
    the probed audio end, even when it appears before another segment in EPUB
    reading order. Clips without segment metadata retain the legacy behavior.
    """
    if not clips:
        return clips

    def same_segment(first: SentenceClip, second: SentenceClip) -> bool:
        if not first.segment_scoped and not second.segment_scoped:
            return True
        return (
            first.segment_scoped
            and second.segment_scoped
            and first.segment_key is not None
            and first.segment_key == second.segment_key
        )

    segment_ends = {
        clip.segment_key: clip.segment_ts_end
        for clip in clips
        if clip.segment_scoped
        and clip.segment_key is not None
        and clip.segment_ts_end is not None
    }
    temporal_last_key = max(segment_ends, key=segment_ends.get) if segment_ends else None

    extended = list(clips)
    for i in range(len(extended) - 1):
        current = extended[i]
        following = extended[i + 1]
        if same_segment(current, following):
            target_end = following.ts_start
        elif current.segment_scoped and current.segment_ts_end is not None:
            target_end = current.segment_ts_end
            if (
                current.segment_key is not None
                and current.segment_key == temporal_last_key
                and audio_duration_seconds is not None
            ):
                target_end = max(target_end, audio_duration_seconds)
        else:
            target_end = current.ts_end
        if target_end >= current.ts_end:
            extended[i] = replace(current, ts_end=target_end)
    last = extended[-1]
    if not last.segment_scoped and audio_duration_seconds is not None and audio_duration_seconds > last.ts_end:
        extended[-1] = replace(last, ts_end=audio_duration_seconds)
    elif last.segment_scoped:
        target_end = last.segment_ts_end
        if last.segment_key is not None and last.segment_key == temporal_last_key:
            target_end = audio_duration_seconds if audio_duration_seconds is not None else target_end
        if target_end is not None and target_end > last.ts_end:
            extended[-1] = replace(last, ts_end=target_end)
    else:
        logger.warning(
            "Read-along: could not extend final clip %s to the real audio "
            "duration (probed=%s, clip end=%.3f); overlay total will run short",
            last.sentence_id, audio_duration_seconds, last.ts_end,
        )
    return extended


def _local_tag_name(tag: object) -> str:
    """Lower-cased tag name without any namespace prefix."""
    return (getattr(tag, "name", None) or "").rsplit(":", 1)[-1].lower()


def _common_ancestor(first: NavigableString, last: NavigableString) -> Optional[Tag]:
    """Lowest element containing both text nodes (their shared parent if equal)."""
    first_chain = set()
    node = first.parent
    while node is not None:
        first_chain.add(id(node))
        node = node.parent
    common = last.parent
    while common is not None and id(common) not in first_chain:
        common = common.parent
    return common


def _range_is_inline_only(first: NavigableString, last: NavigableString) -> bool:
    """Whether the elements strictly between each text node and their common
    ancestor are all inline -- i.e. wrapping the range needs no block-level
    element split or crossed."""
    common = _common_ancestor(first, last)
    if common is None:
        return False
    for start in (first, last):
        node = start.parent
        while node is not None and node is not common:
            if _local_tag_name(node) in _BLOCK_LEVEL_TAGS:
                return False
            node = node.parent
    return True


def _new_marker_span(soup: BeautifulSoup, context: Tag, marker_id: str) -> Tag:
    """Create an empty ``<span id=marker_id>`` in ``context``'s namespace/prefix."""
    namespace = getattr(context, "namespace", None)
    marker = soup.new_tag("span", namespace=namespace) if namespace else soup.new_tag("span")
    prefix = getattr(context, "prefix", None)
    if namespace and prefix:
        marker.prefix = prefix
    marker["id"] = marker_id
    return marker


def _split_parent_around(node: PageElement, before: bool) -> None:
    """Split ``node.parent`` so ``node`` is the first (``before``) or last child
    of the part that stays in place.

    ``before=True`` moves the preceding siblings into a shallow clone (same
    name, namespace, prefix and attributes) inserted before the parent;
    ``before=False`` moves the following siblings into a clone inserted after
    it. Whichever part comes first in document order keeps the element's
    ``id``, so ids stay unique.
    """
    parent = node.parent
    position = next(i for i, child in enumerate(parent.contents) if child is node)
    moved = parent.contents[:position] if before else parent.contents[position + 1:]
    if not moved:
        return
    clone = copy.copy(parent)
    for child in list(clone.contents):
        child.extract()
    if before:
        original_id = parent.attrs.pop("id", None)
        if original_id is not None:
            clone.attrs["id"] = original_id
        parent.insert_before(clone)
    else:
        clone.attrs.pop("id", None)
        parent.insert_after(clone)
    for child in moved:
        clone.append(child)


def _wrap_sentence_range(
    soup: BeautifulSoup, first: NavigableString, last: NavigableString, marker_id: str,
) -> bool:
    """Wrap everything from text node ``first`` to ``last`` (inclusive) in one
    marker span, splitting inline ancestors that straddle the range edges.

    Returns ``False`` (tree untouched) when the two nodes share no ancestor.
    """
    common = _common_ancestor(first, last)
    if common is None:
        return False

    start_child = first
    while start_child.parent is not common:
        _split_parent_around(start_child, before=True)
        start_child = start_child.parent
    end_child = last
    while end_child.parent is not common:
        _split_parent_around(end_child, before=False)
        end_child = end_child.parent

    contents = common.contents
    start_pos = next(i for i, child in enumerate(contents) if child is start_child)
    end_pos = next(i for i, child in enumerate(contents) if child is end_child)
    segment = contents[start_pos:end_pos + 1]
    span = _new_marker_span(soup, common, marker_id)
    start_child.insert_before(span)
    for child in segment:
        span.append(child)
    return True


def _splice_markers(
    soup: BeautifulSoup,
    nodes: List[NavigableString],
    markers: List[_Marker],
    stats: Optional[Dict[str, int]] = None,
) -> None:
    """Wrap each marker's whole sentence range in ``<span id="...">...</span>``
    inside ``soup``, in place.

    ``nodes`` is the exact content-string node list ``markers``' node indices
    were computed against (either ``content_string_nodes(soup)`` for the
    ebooklib-reconstructed-content path, or the ORIGINAL-bytes body-scoped
    node list for the fidelity-preserving path -- see
    :func:`_resolve_spine_injection_target`).

    ``markers`` are ``(start_node_index, start_offset, end_node_index,
    end_offset, marker_id)`` tuples as :func:`_markers_for_spine_item`
    returns them; ``end_offset`` is exclusive and indexes the end node's own
    (unstripped) string.

    Phase A splits every affected text node once at all of its boundary
    offsets (sentence starts and ends alike), so every sentence boundary
    becomes a text-node boundary and each marker can name its first and last
    piece. Phase B wraps, per marker, the contiguous DOM range between those
    pieces in one span (:func:`_wrap_sentence_range`), splitting inline
    ancestors that straddle the range edges so the span holds exactly the
    sentence.

    A sentence whose range would need a block-level element split or crossed
    falls back to wrapping only its start text node (up to the end of that
    node's text); each is counted in ``stats["unwrapped"]`` when ``stats`` is
    given. Markers with out-of-range or reversed ranges are logged and
    skipped. A same-node range with ``end_offset == start_offset`` yields an
    empty span at that position.
    """
    resolved: List[_Marker] = []
    for start_index, start_offset, end_index, end_offset, marker_id in markers:
        if not (0 <= start_index < len(nodes) and 0 <= end_index < len(nodes)):
            logger.error(
                "Marker injection: node_index %d-%d out of range (%d nodes); "
                "skipping marker %s",
                start_index, end_index, len(nodes), marker_id,
            )
            continue
        start_len = len(str(nodes[start_index]))
        end_len = len(str(nodes[end_index]))
        if (
            end_index < start_index
            or (end_index == start_index and end_offset < start_offset)
            or not (0 <= start_offset <= start_len and 0 <= end_offset <= end_len)
        ):
            logger.error(
                "Marker injection: range %d@%d-%d@%d out of order/range "
                "(lengths %d/%d); skipping marker %s",
                start_index, start_offset, end_index, end_offset,
                start_len, end_len, marker_id,
            )
            continue
        if end_index != start_index and not _range_is_inline_only(nodes[start_index], nodes[end_index]):
            end_index = start_index
            end_offset = max(start_offset, len(str(nodes[start_index]).rstrip()))
            if stats is not None:
                stats["unwrapped"] = stats.get("unwrapped", 0) + 1
            logger.debug(
                "Read-along marker: sentence %s crosses a block-level "
                "element boundary; its highlight target covers only the "
                "first text run of the sentence",
                marker_id,
            )
        resolved.append((start_index, start_offset, end_index, end_offset, marker_id))

    boundaries: Dict[int, set] = {}
    for start_index, start_offset, end_index, end_offset, _marker_id in resolved:
        boundaries.setdefault(start_index, set()).add(start_offset)
        boundaries.setdefault(end_index, set()).add(end_offset)

    pieces_by_node: Dict[int, List[Tuple[int, int, NavigableString]]] = {}
    for node_index, offsets in boundaries.items():
        node = nodes[node_index]
        raw = str(node)
        cuts = sorted(o for o in offsets if 0 < o < len(raw))
        if not cuts:
            pieces_by_node[node_index] = [(0, len(raw), node)]
            continue
        edges = [0] + cuts + [len(raw)]
        spans = list(zip(edges, edges[1:]))
        pieces = [NavigableString(raw[a:b]) for a, b in spans]
        node.replace_with(*pieces)
        pieces_by_node[node_index] = [(a, b, piece) for (a, b), piece in zip(spans, pieces)]

    def piece_at(node_index: int, offset: int, starting: bool) -> Optional[NavigableString]:
        for a, b, piece in pieces_by_node[node_index]:
            if b > a and (a if starting else b) == offset:
                return piece
        return None

    for start_index, start_offset, end_index, end_offset, marker_id in resolved:
        first = piece_at(start_index, start_offset, starting=True)
        last = piece_at(end_index, end_offset, starting=False)
        if start_index == end_index and end_offset == start_offset:
            if last is not None:
                last.insert_after(_new_marker_span(soup, last.parent, marker_id))
            elif first is not None:
                first.insert_before(_new_marker_span(soup, first.parent, marker_id))
            continue
        if first is None or last is None or not _wrap_sentence_range(soup, first, last, marker_id):
            logger.error(
                "Marker injection: could not wrap range %d@%d-%d@%d; "
                "skipping marker %s",
                start_index, start_offset, end_index, end_offset, marker_id,
            )


def _inject_markers(
    content: Union[str, bytes],
    markers: List[_Marker],
    stats: Optional[Dict[str, int]] = None,
) -> bytes:
    """Wrap each marker's sentence text in ``<span id="...">`` in one spine
    item's XHTML.

    Re-parses ``content`` independently of ``ebook_dom_map`` (which discards
    its soup after extracting text) with the identical parser
    (``'html.parser'``) and node filter (``content_string_nodes``), so
    ``node_index`` values line up exactly with what :func:`build_dom_anchor_map`
    computed them against.

    This is the fallback path used when a spine item's ORIGINAL archive bytes
    could not be used (see :func:`_resolve_spine_injection_target`) -- it
    reproduces this module's pre-fidelity-fix behaviour exactly (ebooklib's
    lossy reconstruction), so a spine item that fails the fidelity path is no
    worse off than before that fix, just not improved by it.
    """
    soup = BeautifulSoup(content, "html.parser")
    nodes = content_string_nodes(soup)
    _splice_markers(soup, nodes, markers, stats)
    return str(soup).encode("utf-8")


def _inject_markers_into_original(
    soup: BeautifulSoup,
    nodes: List[NavigableString],
    markers: List[_Marker],
    stats: Optional[Dict[str, int]] = None,
) -> bytes:
    """Wrap each marker's sentence text in ``<span id="...">`` inside an
    already-parsed, ORIGINAL-archive-bytes ``soup`` (see
    :func:`_resolve_spine_injection_target`), preserving every attribute,
    tag-name case, and stylesheet link the source document had -- this is the
    fix for Finding 1 (styling/attributes lost via ``spine_map``'s
    ebooklib-reconstructed ``content``).

    ``soup`` must have been parsed with :func:`~src.utils.ebook_dom_map.parse_original_spine_xml`
    (an XML-mode, case-preserving parser) and ``nodes`` must be the exact node
    list ``markers``' node indices were resolved against. ``stats`` receives
    the count of sentences that could not be fully wrapped
    (see :func:`_splice_markers`).
    """
    _splice_markers(soup, nodes, markers, stats)
    return str(soup).encode("utf-8")


def _resolve_spine_injection_target(
    original_bytes: Optional[bytes],
    ref_entry: SpineDomMap,
    expected_text: str,
    reference_content: Optional[Union[str, bytes]] = None,
) -> Optional[Tuple[SpineDomMap, BeautifulSoup, List[NavigableString]]]:
    """Attempt to build a fidelity-preserving injection target for one spine
    item from its ORIGINAL archive bytes (Finding 1's fix).

    Parses ``original_bytes`` with :func:`~src.utils.ebook_dom_map.parse_original_spine_xml`
    (case- and attribute-preserving XML mode), scopes node enumeration to the
    ``<body>`` element, and rebuilds a local run table with
    :func:`~src.utils.ebook_dom_map.runs_from_nodes` -- the same algorithm
    Phase 1 uses, just computed fresh against the ORIGINAL nodes' own
    (possibly differently-whitespaced) raw strings rather than reusing offsets
    computed against ebooklib's reconstructed content, which would only be
    valid if the two documents happened to serialize identical whitespace
    around every text node.

    Returns ``None`` -- never raises -- when ``original_bytes`` is unavailable,
    fails to parse, or its body children cannot be mapped deterministically to
    the canonical content. Direct text owned by ``<body>`` is intentionally
    left unaligned because ebooklib drops it while building the canonical
    content; it remains in the original document that is shipped.

    On success, returns ``(dom_entry, soup, nodes)``: ``dom_entry`` is a new
    ``SpineDomMap`` with the same ``spine_index``/``href``/``start``/``end`` as
    ``ref_entry`` but runs computed fresh from the original document (global-
    offset-shifted, matching :func:`~src.utils.ebook_dom_map.build_dom_anchor_map`'s
    own shifting), ``soup`` is the parsed original document, and ``nodes`` is
    its body-scoped content-string node list -- both needed by
    :func:`_inject_markers_into_original`.
    """
    if not original_bytes:
        return None
    soup = parse_original_spine_xml(original_bytes)
    if soup is None:
        return None
    body = original_body_scope(soup)
    nodes = content_string_nodes(body)
    local_runs = runs_from_nodes(nodes)
    if reference_content is None:
        reference_soup = None
    else:
        reference_soup = BeautifulSoup(reference_content, "html.parser")

    if reference_soup is None:
        if joined_text(nodes, local_runs) != expected_text:
            return None
        canonical_runs = ref_entry.runs
        mapped_runs = list(zip(canonical_runs, local_runs))
        if len(mapped_runs) != len(canonical_runs):
            return None
    else:
        canonical_body = original_body_scope(reference_soup)
        # Keep the full canonical node list: ``ref_entry.node_index`` was
        # computed over the complete reconstructed document, including its
        # whitespace nodes. Only the path scope is body-relative.
        canonical_nodes = content_string_nodes(reference_soup)
        canonical_runs = runs_from_nodes(canonical_nodes)
        if joined_text(canonical_nodes, canonical_runs) != expected_text:
            return None
        if len(canonical_runs) != len(ref_entry.runs):
            return None

        def node_path(node: NavigableString, scope: object) -> Tuple[Tuple[str, int], ...]:
            path: List[Tuple[str, int]] = []
            parent = node.parent
            while parent is not None and parent is not scope:
                grandparent = parent.parent
                if grandparent is None:
                    return ()
                siblings = [
                    child for child in grandparent.children
                    if getattr(child, "name", None) is not None
                ]
                sibling_index = next((i for i, child in enumerate(siblings) if child is parent), -1)
                if sibling_index < 0:
                    return ()
                path.append((str(getattr(parent, "name", "")).lower(), sibling_index))
                parent = grandparent
            return tuple(reversed(path))

        # Keyed by (structural path to the immediate parent tag, run text) --
        # NOT unique. Two lone runs with identical text under the very same
        # parent are a real, unremarkable shape in prose: e.g.
        # ``<p>&mdash;<span class="epub-i">interruption</span>&mdash;</p>``
        # (a dialogue interruption) produces two separate text-node children
        # of the same ``<p>``, both stripping to the single character "--".
        # ``node_path`` intentionally tracks only *element* ancestry (which
        # ``<p>`` this is), not this text node's own position among its
        # parent's children, so both dashes collide on the same key. Treating
        # that as unresolvable (requiring exactly one candidate) refused the
        # entire spine item -- and therefore the entire build -- over a
        # correctly-matchable paragraph. Since both ``local_runs`` and
        # ``canonical_runs`` are built by a single document-order walk
        # (``runs_from_nodes`` only ever advances forward), the Nth time a key
        # recurs in canonical order corresponds to the Nth candidate recorded
        # for that key in original order, as long as the two documents agree
        # on structure -- which the node/text-count and per-run equality
        # checks around this loop already establish. Candidates are therefore
        # consumed in FIFO order per key instead of demanding a single match.
        original_by_key: Dict[Tuple[Tuple[Tuple[str, int], ...], str], List[DomRun]] = {}
        for run in local_runs:
            key = (node_path(nodes[run.node_index], body), run.text)
            original_by_key.setdefault(key, []).append(run)

        mapped_runs = []
        previous_node_index = -1
        consumed_by_key: Dict[Tuple[Tuple[Tuple[str, int], ...], str], int] = {}
        for canonical_run, ref_run in zip(canonical_runs, ref_entry.runs):
            if (canonical_run.node_index, canonical_run.text) != (ref_run.node_index, ref_run.text):
                return None
            key = (node_path(canonical_nodes[canonical_run.node_index], canonical_body), canonical_run.text)
            candidates = original_by_key.get(key, [])
            next_index = consumed_by_key.get(key, 0)
            if next_index >= len(candidates):
                return None
            candidate = candidates[next_index]
            if candidate.node_index <= previous_node_index:
                return None
            consumed_by_key[key] = next_index + 1
            mapped_runs.append((ref_run, candidate))
            previous_node_index = candidate.node_index

    # `ref_run.start`/`ref_run.end` are already GLOBAL offsets: `ref_entry.runs`
    # comes from `build_dom_anchor_map`, which stores `start=spine_start + local_run.start`
    # (ebook_dom_map.py's own shift). Adding `ref_entry.start` again here doubled
    # the shift for every spine item after the first one (`ref_entry.start != 0`),
    # so `locate_offset` could never find these runs inside their own item's
    # `[start, end)` range -- every clip in that spine item silently dropped as
    # "no DOM location" while earlier, zero-offset spine items (where the bug
    # was invisible: `0 + x == x`) built fine, so the build reported success
    # with a whole chapter missing its narration.
    global_runs = [
        DomRun(
            node_index=original_run.node_index,
            node_offset_start=original_run.node_offset_start,
            node_offset_end=original_run.node_offset_end,
            text=ref_run.text,
            start=ref_run.start,
            end=ref_run.end,
        )
        for ref_run, original_run in mapped_runs
    ]
    dom_entry = SpineDomMap(
        spine_index=ref_entry.spine_index,
        href=ref_entry.href,
        start=ref_entry.start,
        end=ref_entry.end,
        runs=global_runs,
        node_count=len(nodes),
    )
    return dom_entry, soup, nodes


def _xml_wellformed(data: bytes) -> bool:
    """Whether ``data`` parses as well-formed XML."""
    try:
        etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        return True
    except etree.XMLSyntaxError:
        return False


def _merge_adjacent_split_inlines(parent: Tag) -> None:
    """Re-join adjacent sibling inline elements that differ at most by ``id``.

    Marker injection splits an inline element (``<i>``, ``<a>``, ...) that
    straddles a sentence edge into two adjacent copies. Left as is, their
    texts would be joined with the extraction separator instead of staying
    one string, which could differ from the unsplit original wherever the
    split point has no plain space. :func:`_verify_marker_injection` applies
    this to BOTH sides, so it only neutralises where an element was cut.
    """
    child = parent.contents[0] if parent.contents else None
    while child is not None:
        if isinstance(child, Tag):
            following = child.next_sibling
            while (
                isinstance(following, Tag)
                and child.name == following.name
                and _local_tag_name(child) not in _BLOCK_LEVEL_TAGS
                and {k: v for k, v in child.attrs.items() if k != "id"}
                == {k: v for k, v in following.attrs.items() if k != "id"}
            ):
                for moved in list(following.contents):
                    child.append(moved)
                following.decompose()
                following = child.next_sibling
            _merge_adjacent_split_inlines(child)
        child = child.next_sibling


def _verify_marker_injection(original: bytes, modified: bytes, spine_index: int, href: str) -> None:
    """Confirm marker injection did not alter this spine item's extracted text
    and did not turn well-formed XML into malformed XML.

    A marker span wraps a real slice of what was previously one contiguous
    text node, so ``get_text()`` -- which strips each *distinct*
    ``NavigableString`` individually before joining survivors with its own
    canonical single-space separator (see ``ebook_dom_map``'s module
    docstring) -- would otherwise compare the split pieces' individually
    re-stripped text against the original's single, once-stripped text. That
    silently normalizes whatever literal separator character(s) sat at the
    split point (a non-breaking space, a double space, a tab, ...) to bs4's
    own canonical single space, a false mismatch that has nothing to do with
    whether injection actually altered anything. ``canonical_text`` undoes
    the split before comparing instead of relying on ``get_text()``'s own
    joining: it unwraps every id-bearing span (``Tag.unwrap()`` -- replace
    the tag with its own children, in place; a no-op content-wise for an
    empty span, so this subsumes the previous design's decompose-if-empty
    special case) and then ``smooth()``s the tree, which re-merges the
    resulting adjacent ``NavigableString`` siblings back into the exact
    original combined string, whitespace and all -- because injection only
    ever adds tag structure around existing text, never reorders or drops a
    character. A pre-existing, unrelated ``<span id="...">`` already present
    in the source (not one this phase added) is unwrapped identically on both
    sides of the comparison, so it cannot introduce a false mismatch either.

    **A second, independent whitespace pitfall, found live on a real book**
    ("The Incest Nightclub", ``bookorbit:6051``, refused entirely before this
    fix). ``canonical_text`` has to *parse* ``modified`` from scratch, and
    bs4's ``BeautifulSoup.endData()`` collapses any data segment made
    **entirely** of ``BeautifulSoup.ASCII_SPACES`` characters to a single
    space at PARSE time -- before ``unwrap``/``smooth`` ever run. The gap
    between two sentences is never its own segment in the ORIGINAL markup (it
    sits inside one larger text node with real words either side), so it is
    never collapsed there; marker injection routinely isolates exactly that
    gap as a lone whitespace-only node between two new ``<span>``s, which
    *is* entirely ASCII whitespace and *does* get collapsed. The two
    canonical texts then differ by nothing but a run length that was never
    semantically significant, and the whole book was refused over it.

    Measured against the installed bs4 rather than assumed -- and the
    measurement corrects the defect's original description. It is ORDINARY
    **ASCII** whitespace that breaks: an everyday double space after a full
    stop, or a tab. A non-breaking space is NOT in ``ASCII_SPACES``, is not
    collapsed, and already compared equal via the unwrap/smooth fix above.

    Collapsing every ASCII-whitespace run to one space on **both** sides
    (:data:`_ASCII_WHITESPACE_RUN_RE`) makes the comparison insensitive to
    which side bs4 happened to collapse, without hiding an actual content
    change: a dropped word, an altered character or reordered text is never a
    pure whitespace-run-length difference.

    Raises rather than silently shipping a book whose markers landed in the
    wrong place or corrupted surrounding text.

    The well-formedness check is differential: if the *original* content was
    not well-formed XML to begin with (a real-world EPUB using HTML-only
    markup bs4's lenient parser already tolerates), that is a pre-existing
    condition outside this phase's scope, not a regression -- only a
    previously-well-formed document turning invalid here raises.
    """
    def canonical_text(content: bytes) -> str:
        soup = BeautifulSoup(content, "html.parser")
        for span in list(soup.find_all("span")):
            if span.get("id") is not None:
                span.unwrap()
        _merge_adjacent_split_inlines(soup)
        soup.smooth()

        # Preserve preformatted text verbatim while normalizing collapsible
        # whitespace in ordinary prose below.
        protected_pre: List[str] = []
        for pre in soup.find_all(
            lambda tag: getattr(tag, "name", "").rsplit(":", 1)[-1].lower() == "pre"
        ):
            protected_pre.append(pre.get_text())
            placeholder = f"\x00PRE{len(protected_pre) - 1}\x00"
            pre.clear()
            pre.append(placeholder)

        text = soup.get_text(separator=" ", strip=True)
        # Collapse ASCII-whitespace runs uniformly on BOTH sides -- see this
        # function's docstring. Cannot hide a real content change: a dropped
        # word, an altered character or reordered text is never a pure
        # whitespace-run-length difference.
        text = _ASCII_WHITESPACE_RUN_RE.sub(" ", text)
        for index, original in enumerate(protected_pre):
            text = text.replace(f"\x00PRE{index}\x00", original)
        return text

    original_text = canonical_text(original)
    modified_text = canonical_text(modified)
    if original_text != modified_text:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(original_text, modified_text)) if a != b),
            min(len(original_text), len(modified_text)),
        )
        logger.error(
            "Marker injection changed spine item %s (href=%s) text at offset %d",
            spine_index, href, first_diff,
        )
        raise ValueError(
            f"Marker injection altered extracted text for spine_index={spine_index} "
            f"href={href!r} at offset {first_diff}"
        )

    if _xml_wellformed(original) and not _xml_wellformed(modified):
        logger.error(
            "Marker injection produced invalid XML for spine item %s (href=%s)",
            spine_index, href,
        )
        raise ValueError(
            f"Marker injection produced invalid XML for spine_index={spine_index} "
            f"href={href!r}"
        )


@dataclass(frozen=True)
class _PlacedClip:
    """One SMIL ``<par>``'s worth of data after Phase 4 Part C's per-file
    placement: a sentence's already-allocated marker id, its clip times
    made relative to whichever physical audio file it was placed in (see
    the module docstring's "Phase 4 Part C" section), and that file's own
    href, relative to the SMIL document that will reference it.

    Distinct from :class:`~src.services.readalong_segments.SentenceClip`
    (which this module never mutates in place -- ``ts_start``/``ts_end``
    there are always absolute against the single, whole-book embedded
    timeline) specifically so a clip spanning a file boundary in the
    caller's own bookkeeping can never be confused with one whose times are
    already file-relative.
    """
    sentence_id: str
    ts_start: float
    ts_end: float
    audio_href: str


def _build_smil(
    chapter_id: str,
    xhtml_href_from_smil: str,
    placed_clips: List[_PlacedClip],
) -> bytes:
    """Build one spine item's SMIL media-overlay document.

    ``xhtml_href_from_smil`` and each ``placed_clips[i].audio_href`` are
    decoded, filesystem-style paths relative to the SMIL document's own
    location (computed by the caller via ``posixpath.relpath``); this
    function percent-encodes them (:func:`_encode_href_path`, Finding 4)
    before embedding them as ``src``/``epub:textref`` attribute values,
    since a raw filename containing e.g. a space is not a valid URI
    reference. Every ``<par>`` carries both ``clipBegin`` and ``clipEnd`` --
    required so BookOrbit's own duration inspector does not collapse the
    whole overlay to a null total (see this module's docstring).

    **Phase 4 Part C:** different ``<par>``s in the same document can (and
    for a long book routinely do) carry different ``audio_href`` values --
    one SMIL seq is still exactly one spine item's overlay, but its
    sentences' audio may span more than one physical embedded file.
    ``placed_clips``' own ``ts_start``/``ts_end`` are already relative to
    whichever file each one was placed in (the caller's job, not this
    function's).
    """
    xhtml_href_from_smil = _encode_href_path(xhtml_href_from_smil)

    nsmap = {None: _SMIL_NS, "epub": _OPS_NS}
    smil = etree.Element(f"{{{_SMIL_NS}}}smil", nsmap=nsmap, attrib={"version": "3.0"})
    body = etree.SubElement(smil, f"{{{_SMIL_NS}}}body")
    seq = etree.SubElement(
        body,
        f"{{{_SMIL_NS}}}seq",
        attrib={
            "id": f"{chapter_id}_overlay",
            f"{{{_OPS_NS}}}textref": xhtml_href_from_smil,
            f"{{{_OPS_NS}}}type": "bodymatter chapter",
        },
    )
    for clip in placed_clips:
        ts_start = float(clip.ts_start)
        ts_end = float(clip.ts_end)
        if ts_end < ts_start:
            logger.warning(
                "SMIL par %s: clipEnd %.3f < clipBegin %.3f, clamping",
                clip.sentence_id, ts_end, ts_start,
            )
            ts_end = ts_start
        par = etree.SubElement(seq, f"{{{_SMIL_NS}}}par", attrib={"id": clip.sentence_id})
        etree.SubElement(
            par,
            f"{{{_SMIL_NS}}}text",
            attrib={"src": f"{xhtml_href_from_smil}#{clip.sentence_id}"},
        )
        etree.SubElement(
            par,
            f"{{{_SMIL_NS}}}audio",
            attrib={
                "src": _encode_href_path(clip.audio_href),
                "clipBegin": f"{ts_start:.3f}s",
                "clipEnd": f"{ts_end:.3f}s",
            },
        )
    return etree.tostring(smil, xml_declaration=True, encoding="utf-8", standalone=False)


def _rewrite_opf(
    opf_bytes: bytes,
    opf_dir: str,
    overlays: List[SpineOverlayResult],
    audio_files: List[Tuple[str, str, str]],
    total_duration: float,
) -> bytes:
    """Add media-overlay manifest items and ``media:duration`` metadata to an OPF.

    Everything else in the OPF is preserved exactly as parsed -- this edits
    the existing tree in place with lxml (which keeps comments, processing
    instructions, attribute order and untouched elements' formatting intact)
    rather than rebuilding the document.

    ``audio_files`` is one ``(manifest_href, media_type, manifest_id)`` tuple
    per physical embedded audio file (Phase 4 Part C) -- ``manifest_href`` is
    the decoded, OPF-dir-relative path this function encodes itself, same as
    every other href here. Usually one entry; several for a long book.

    Raises ``ValueError`` if the OPF has no ``<manifest>``/``<metadata>`` to
    attach overlays to, or (Finding 4, independent review) if a spine item's
    overlay cannot be matched to its own manifest ``<item>`` -- the caller
    treats either as a refusal, not a crash. A manifest ``href`` attribute is
    a URI reference and may be percent-encoded (``chapter%201.xhtml`` for an
    archive member literally named ``chapter 1.xhtml``); comparing it
    undecoded against ``overlay.href`` (always the plain, decoded archive
    path -- see ``extract_text_and_map``'s ``href_resolver``) would silently
    never match for such a book, producing a "successful" build with no
    media overlay registered for that spine item. This decodes each
    manifest ``href`` before comparing.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.fromstring(opf_bytes, parser=parser)
    manifest = tree.find(f"{{{_OPF_NS}}}manifest")
    metadata = tree.find(f"{{{_OPF_NS}}}metadata")
    if manifest is None or metadata is None:
        raise ValueError("OPF is missing <manifest> or <metadata>; cannot attach read-along overlays")

    existing_ids = {item.get("id") for item in manifest.findall(f"{{{_OPF_NS}}}item") if item.get("id")}

    href_to_item = {}
    for item in manifest.findall(f"{{{_OPF_NS}}}item"):
        href_attr = item.get("href")
        if not href_attr:
            continue
        decoded_href_attr = unquote(href_attr)
        archive_href = (
            posixpath.normpath(posixpath.join(opf_dir, decoded_href_attr))
            if opf_dir else posixpath.normpath(decoded_href_attr)
        )
        href_to_item[archive_href] = item

    def _append_with_tail(parent: etree._Element, child: etree._Element) -> None:
        """Append ``child`` and copy a sibling's tail whitespace onto it, so
        the new element does not land squished onto the closing tag's line."""
        if len(parent) > 0:
            child.tail = parent[-1].tail
        parent.append(child)

    for overlay in overlays:
        target = href_to_item.get(overlay.href)
        if target is None:
            logger.error(
                "Read-along OPF rewrite: no manifest <item> found for spine href "
                "'%s'; refusing rather than silently producing a book with no "
                "media overlay for this spine item",
                overlay.href,
            )
            raise ValueError(
                f"No manifest <item> found for spine href {overlay.href!r}; "
                "cannot attach its media overlay"
            )

        smil_item_id = _unique_manifest_id(existing_ids, f"c{overlay.spine_index}-overlay")
        existing_ids.add(smil_item_id)
        target.set("media-overlay", smil_item_id)

        smil_manifest_href = _encode_href_path(posixpath.relpath(overlay.smil_href, opf_dir or "."))
        smil_item = etree.Element(
            f"{{{_OPF_NS}}}item",
            attrib={"id": smil_item_id, "href": smil_manifest_href, "media-type": "application/smil+xml"},
        )
        _append_with_tail(manifest, smil_item)

        duration_meta = etree.Element(
            f"{{{_OPF_NS}}}meta",
            attrib={"refines": f"#{smil_item_id}", "property": "media:duration"},
        )
        duration_meta.text = _format_smil_clock(overlay.duration_seconds)
        _append_with_tail(metadata, duration_meta)

    for audio_manifest_href, audio_media_type, audio_manifest_id in audio_files:
        audio_item = etree.Element(
            f"{{{_OPF_NS}}}item",
            attrib={
                "id": audio_manifest_id,
                "href": _encode_href_path(audio_manifest_href),
                "media-type": audio_media_type,
            },
        )
        _append_with_tail(manifest, audio_item)

    total_meta = etree.Element(f"{{{_OPF_NS}}}meta", attrib={"property": "media:duration"})
    total_meta.text = _format_smil_clock(total_duration)
    _append_with_tail(metadata, total_meta)

    declares_active_class = any(
        meta.get("property") == "media:active-class"
        for meta in metadata.findall(f"{{{_OPF_NS}}}meta")
    )
    if not declares_active_class:
        active_class_meta = etree.Element(f"{{{_OPF_NS}}}meta", attrib={"property": "media:active-class"})
        active_class_meta.text = _MEDIA_OVERLAY_ACTIVE_CLASS
        _append_with_tail(metadata, active_class_meta)

    return etree.tostring(tree, xml_declaration=True, encoding="utf-8", standalone=False)


def _package_epub(
    source_epub: Union[str, Path],
    output_path: Union[str, Path],
    modified_files: Dict[str, bytes],
    new_bytes_files: Dict[str, bytes],
    new_disk_files: Dict[str, Path],
) -> None:
    """Repackage an EPUB with modified/added files, everything else untouched.

    Per the EPUB OCF spec: ``mimetype`` is written first, stored uncompressed
    (``ZIP_STORED``), containing exactly ``application/epub+zip``. Every other
    original entry is copied byte-for-byte with its original compression
    method, unless overridden by ``modified_files``. ``new_bytes_files``
    (generated SMIL) are appended deflated; ``new_disk_files`` (the embedded
    audio) are streamed from disk with ``ZipFile.write`` rather than loaded
    into memory, stored uncompressed since audio is already compressed.

    **Finding 5 (independent review):** raises ``ValueError`` -- without
    touching either file -- when ``output_path`` and ``source_epub`` are the
    same file. Opening the destination with ``zipfile.ZipFile(path, "w")``
    truncates it immediately, but ``source_epub``'s own ``ZipFile`` is still
    reading from that same underlying file for the copy loop below; a real
    reproduction of this exact call with aliased paths truncated the source
    to its first entry and then raised ``BadZipFile: Truncated file header``
    reading the second one -- destroying the caller's only copy. Even for
    non-aliased paths, ``output_path`` is never opened directly: the archive
    is built into a temporary sibling file first and only ``os.replace()``d
    onto ``output_path`` once fully and successfully written, so a failure
    partway through (a disk-full ``OSError``, a KeyboardInterrupt, ...) never
    leaves a truncated or half-written file at the real destination -- the
    previous ``output_path``, if any, is left completely untouched.
    """
    source_epub = Path(source_epub)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if source_epub.resolve() == output_path.resolve():
        raise ValueError(
            f"Read-along packaging refuses to write '{output_path}' over its "
            "own source EPUB -- opening the destination for writing would "
            "truncate the file the source archive is still being read from"
        )

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=str(output_path.parent),
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    # tempfile.mkstemp() deliberately creates the file mode 0600 (owner-only)
    # for security when the destination might be a shared/multi-user
    # location. This file's destination is a generated artifact meant to be
    # read by OTHER processes entirely (BookOrbit's own scanner, running in
    # a different container) -- os.replace() preserves whatever mode the
    # temp file had, so leaving it at 0600 would silently ship a read-along
    # EPUB unreadable outside this container. Match the permissive mode a
    # plain zipfile.ZipFile(path, "w") would have produced.
    os.chmod(tmp_path, 0o644)
    try:
        with zipfile.ZipFile(source_epub) as src, zipfile.ZipFile(tmp_path, "w") as dst:
            dst.writestr(
                zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0)),
                b"application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )

            for name in src.namelist():
                if name == "mimetype":
                    continue
                info = src.getinfo(name)
                data = modified_files.get(name, src.read(name))
                new_info = zipfile.ZipInfo(name, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                dst.writestr(new_info, data)

            for name, data in new_bytes_files.items():
                dst.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

            for name, disk_path in new_disk_files.items():
                dst.write(str(disk_path), arcname=name, compress_type=zipfile.ZIP_STORED)

        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            logger.warning(
                "Read-along packaging: could not remove temporary file '%s' "
                "after a failed build: %s", tmp_path, cleanup_error, exc_info=True,
            )
        raise


@contextmanager
def _resolve_epub3_source(
    epub_path: Path, abs_id: str,
    progress_callback: Optional[ReadalongProgressCallback] = None,
) -> Iterator[Optional[Path]]:
    """Yield an EPUB 3 source path for ``epub_path``, converting in a private
    temporary copy if it is EPUB 2 -- never modifies ``epub_path`` itself
    (see ``src/services/epub3_upgrade.py``'s module docstring on why that
    conversion never touches spine content and so cannot invalidate an
    alignment map fitted against the original file).

    Yields the original ``epub_path`` unchanged (no copying at all) when it
    is already EPUB 3 -- the common case for the 63 books that predate this
    conversion. Yields ``None`` if ``epub_path`` is EPUB 2 and
    :func:`~src.services.epub3_upgrade.upgrade_epub2_to_epub3` refuses to
    convert it; the caller must treat that the same as any other build
    refusal. The temporary conversion copy, if one was made, is removed on
    exit regardless of outcome.

    Reports the ``'converting_epub'`` stage to ``progress_callback`` (see
    ``_safe_progress``) as soon as this function starts, whether or not an
    actual conversion turns out to be needed -- it is cheap either way, but
    the version check and, on EPUB 2, the conversion itself are still real
    file I/O worth distinguishing from the parsing/transcoding stages that
    follow.
    """
    _safe_progress(progress_callback, "converting_epub", _STAGE_START["converting_epub"])
    version: Optional[str] = None
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_path = _find_opf_path(zf)
            if opf_path and opf_path in set(zf.namelist()):
                version = _opf_package_version(zf.read(opf_path))
    except (OSError, zipfile.BadZipFile) as e:
        logger.warning(
            "Read-along build: could not read '%s' to check its EPUB "
            "package version: %s", epub_path, e, exc_info=True,
        )

    if version and version.startswith("3"):
        yield epub_path
        return

    with tempfile.TemporaryDirectory(prefix="readalong-epub3-") as tmp_dir:
        converted_path = Path(tmp_dir) / f"{epub_path.stem}.epub3.epub"
        result = upgrade_epub2_to_epub3(epub_path, converted_path)
        if result is None:
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': could not "
                "upgrade source EPUB (package version %s) to EPUB 3",
                abs_id, version or "<missing>",
            )
            yield None
            return
        logger.info(
            "📖 Converted EPUB 2 source to EPUB 3 for read-along build '%s' "
            "(%d TOC entries, %d landmarks) before assembly",
            abs_id, result.toc_entry_count, result.landmark_count,
        )
        yield converted_path


def build_readalong_epub(
    parser: "EbookParser",
    alignment_service: "AlignmentService",
    epub_path: Union[str, Path],
    audio_paths: Union[str, Path, Sequence[Union[str, Path]]],
    abs_id: str,
    output_path: Union[str, Path],
    standalone_audio_output_path: Optional[Union[str, Path]] = None,
    progress_callback: Optional[ReadalongProgressCallback] = None,
) -> Optional[ReadalongBuildResult]:
    """Assemble a read-along EPUB 3 (SMIL media overlays) for one book.

    **EPUB 2 source is converted, not refused** (see
    ``src/services/epub3_upgrade.py``): this thin wrapper resolves
    ``epub_path`` to an EPUB 3 file -- the original if it already is one, or
    a private temporary conversion copy that is cleaned up when the build
    finishes -- via :func:`_resolve_epub3_source`, then delegates to
    :func:`_build_readalong_epub_impl` for the actual assembly. The original
    file at ``epub_path`` is never modified either way.

    Combines Phase 1's DOM anchor map with Phase 2's sentence/clip table to:
    inject an empty ``<span id="...">`` marker at each sentence's start
    position in its spine item's XHTML, emit one ``.smil`` per spine item that
    has sentences, add the SMIL/audio manifest items and ``media:duration``
    metadata to the OPF, and repackage as a new EPUB.

    The source EPUB is otherwise untouched: every existing manifest item,
    spine entry, and file is carried through byte-for-byte except the handful
    of spine XHTML documents that receive markers and the OPF itself.

    **Phase 4 Part A:** every emitted clip's end is extended to the next
    emitted clip's start (:func:`_extend_clips_to_contiguous`), across
    spine-item boundaries, so playback never goes dark between sentences and
    the summed overlay duration tracks the real audio instead of running
    short by every inter-sentence pause. The book's last clip is extended to
    the real, probed duration of whatever audio actually gets embedded.

    **Phase 4 Part B:** ``audio_paths`` is prepared as AAC according to
    ``READALONG_AUDIO_BITRATE`` (see :func:`_resolve_audio_bitrate`). Source
    mode preserves compatible single-file AAC. See
    :func:`_transcode_audio_for_embed` for the transcoding fallback and why
    multi-file concatenation preserves the alignment map's timeline.

    Returns ``None`` (refuses, does not raise) rather than emit a broken or
    empty book when: ``audio_paths`` is empty; Phase 2's fitted-EPUB guard
    refuses the stored alignment map (see
    ``readalong_segments.build_sentence_clips``); no spine item's sentences
    could be anchored in the DOM at all; the audio transcode fails; or the
    EPUB's OPF has no ``<manifest>``/``<metadata>`` to attach overlays to.

    :param parser: source of the book's spine text/DOM (shared with Phases 1/2).
    :param alignment_service: source of the book's stored alignment map.
    :param epub_path: path to the source EPUB.
    :param audio_paths: path to the source audio, or an ordered list of parts
        for a multi-file audiobook -- must be in the same order the book was
        force-aligned against.
    :param abs_id: the book's ABS id (the alignment map's primary key).
    :param output_path: where to write the generated EPUB.
    :param standalone_audio_output_path: when given, the transcoded embed
        audio is also copied here -- BookOrbit's file scanner only recognizes
        a media-overlay EPUB's audio when a standalone copy sits beside it in
        the same library entry (Phase 3's live finding); this lets a caller
        (or Phase 5's delivery step) get that sibling file from the exact
        same transcode this build already paid for, instead of running
        ffmpeg a second time.
    :param progress_callback: optional ``(stage, fraction)`` reporter for the
        long-running stages below -- see the module-level ``_STAGE_START``
        map and ``_safe_progress``. A failure in the callback itself never
        aborts the build.
    :return: the build result, or ``None`` if refused.
    """
    epub_path = Path(epub_path)

    # Defect 1 (independent review): this aliasing check must run against the
    # ORIGINAL source path, before any EPUB 2 -> EPUB 3 conversion, and before
    # _resolve_epub3_source ever opens a temporary file. _package_epub's own
    # aliasing guard (Finding 5) compares whatever epub_path it is actually
    # handed against output_path -- for an EPUB 2 source that is a private
    # temporary conversion copy, never the original, so calling this public
    # entry point with output_path == the original EPUB 2 library path sailed
    # straight through that check and _package_epub then overwrote the
    # user's real library file via os.replace(). Refusing here, before
    # _resolve_epub3_source is even entered, means a refused build never
    # touches the filesystem at all.
    if epub_path.resolve() == Path(output_path).resolve():
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': output_path '%s' "
            "is the same file as the source EPUB -- writing the generated "
            "read-along package there would overwrite the original library "
            "book (Defect 1, independent review)",
            abs_id, output_path,
        )
        return None

    with _resolve_epub3_source(epub_path, abs_id, progress_callback) as resolved_epub_path:
        if resolved_epub_path is None:
            return None
        return _build_readalong_epub_impl(
            parser, alignment_service, resolved_epub_path, audio_paths, abs_id,
            output_path, standalone_audio_output_path, progress_callback,
        )


def _build_readalong_epub_impl(
    parser: "EbookParser",
    alignment_service: "AlignmentService",
    epub_path: Union[str, Path],
    audio_paths: Union[str, Path, Sequence[Union[str, Path]]],
    abs_id: str,
    output_path: Union[str, Path],
    standalone_audio_output_path: Optional[Union[str, Path]] = None,
    progress_callback: Optional[ReadalongProgressCallback] = None,
) -> Optional[ReadalongBuildResult]:
    """The actual read-along assembly, run against an ``epub_path`` already
    guaranteed to be EPUB 3 -- see :func:`build_readalong_epub`, the public
    entry point, for the EPUB 2 conversion step and the full docstring this
    function shares. Kept as a separate function purely so that conversion's
    temporary-directory lifetime (:func:`_resolve_epub3_source`) can wrap a
    single delegating call instead of this whole body.
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    source_audio_paths = _normalize_audio_paths(audio_paths)
    if not source_audio_paths:
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': no audio paths given",
            abs_id,
        )
        return None

    _safe_progress(progress_callback, "parsing_epub", _STAGE_START["parsing_epub"])
    clip_result = build_sentence_clips(parser, epub_path, alignment_service, abs_id)
    if clip_result is None or not clip_result.clips:
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': no sentence clips available",
            abs_id,
        )
        return None

    dom_map = build_dom_anchor_map(parser, epub_path)
    combined_text, spine_map = parser.extract_text_and_map(epub_path)
    content_by_spine = {entry["spine_index"]: entry["content"] for entry in spine_map}
    href_by_spine = {entry["spine_index"]: entry["href"] for entry in spine_map}

    clips_by_spine: Dict[int, List[SentenceClip]] = {}
    for clip in clip_result.clips:
        clips_by_spine.setdefault(clip.spine_index, []).append(clip)

    # Finding 1 fix: read each candidate spine item's ORIGINAL archive bytes
    # too (not just ebooklib's lossy `content` reconstruction), while the zip
    # is open, so injection below can prefer them -- see
    # _resolve_spine_injection_target. A missing/unreadable entry here just
    # means that one spine item falls back to the pre-existing
    # reconstructed-content path.
    original_bytes_by_spine: Dict[int, bytes] = {}
    with zipfile.ZipFile(epub_path) as zf:
        zip_names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        if not opf_path or opf_path not in zip_names:
            logger.error(
                "Read-along build: could not locate OPF in '%s' (abs_id=%s)",
                epub_path, abs_id,
            )
            return None
        opf_bytes = zf.read(opf_path)

        opf_version = _opf_package_version(opf_bytes)
        if not opf_version or not opf_version.startswith("3"):
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': source EPUB "
                "package version is %s, not EPUB 3 (Finding 2, independent "
                "review) -- appending SMIL media overlays to an EPUB 2 "
                "package without upgrading it to EPUB 3 navigation/metadata "
                "produces a package that is not EPUB-3-conformant, even if a "
                "reader happens to play the overlay anyway",
                abs_id, opf_version or "<missing>",
            )
            return None

        if _opf_has_media_overlays(opf_bytes):
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': source EPUB "
                "already has media overlays (Defect 2, independent review) "
                "-- this builder always appends its own new publication-level "
                "media:duration, so building from a source that already has "
                "one would leave two, which EPUB 3 disallows (exactly one is "
                "permitted); a Storyteller-produced read-along fed back into "
                "this builder is the practical case",
                abs_id,
            )
            return None

        for spine_index in clips_by_spine:
            href = href_by_spine.get(spine_index)
            if href and href in zip_names:
                try:
                    original_bytes_by_spine[spine_index] = zf.read(href)
                except (KeyError, zipfile.BadZipFile) as e:
                    logger.warning(
                        "Read-along fidelity: could not read original archive "
                        "bytes for spine item %d (href=%s): %s",
                        spine_index, href, e, exc_info=True,
                    )

    opf_dir = posixpath.dirname(opf_path)
    readalong_dir = _unique_archive_dir(zip_names, opf_dir, _READALONG_DIR_BASE)

    # Finding 1 fix: every spine item with sentences must inject into its
    # ORIGINAL archive bytes. A reconstructed fallback can silently discard
    # styles, attributes, or body text, so an item that cannot be mapped is a
    # whole-build refusal.
    effective_dom_map: List[SpineDomMap] = []
    injection_target_by_spine: Dict[int, Tuple[BeautifulSoup, List[NavigableString]]] = {}
    for entry in dom_map:
        if entry.spine_index not in clips_by_spine:
            effective_dom_map.append(entry)
            continue
        resolved = _resolve_spine_injection_target(
            original_bytes_by_spine.get(entry.spine_index),
            entry,
            combined_text[entry.start:entry.end],
            content_by_spine.get(entry.spine_index),
        )
        if resolved is None:
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': could not "
                "map original XHTML for spine item %d",
                abs_id, entry.spine_index,
            )
            return None
        dom_entry, soup, nodes = resolved
        effective_dom_map.append(dom_entry)
        injection_target_by_spine[entry.spine_index] = (soup, nodes)

    # Pass 1: DOM-locate every clip's marker per spine item. This determines
    # the actual, final sequence of sentences that will become SMIL <par>s (a
    # sentence Phase 2 timestamped but this phase can't place in the DOM is
    # dropped here) -- cheap, and worth resolving before paying for a
    # (potentially multi-minute, for a long audiobook) transcode below.
    located_by_spine: Dict[int, List[SentenceClip]] = {}
    markers_by_spine: Dict[int, List[_Marker]] = {}
    marker_id_map_by_spine: Dict[int, Dict[str, str]] = {}
    dropped_no_location = 0
    crossed_inline_boundary = 0
    for spine_index, clips in sorted(clips_by_spine.items()):
        href = href_by_spine.get(spine_index)
        content = content_by_spine.get(spine_index)
        if href is None or content is None:
            logger.error(
                "Read-along build: spine index %d has clips but no spine_map "
                "entry (abs_id=%s); skipping",
                spine_index, abs_id,
            )
            continue
        if href not in zip_names:
            logger.error(
                "Read-along build: spine href '%s' is not an archive entry "
                "(abs_id=%s); skipping spine item %d",
                href, abs_id, spine_index,
            )
            continue

        # Finding 6 fix: collision-free marker ids in the original markup.
        id_scan_source = original_bytes_by_spine.get(spine_index)
        if id_scan_source is None:
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': original "
                "XHTML is unavailable for spine item %d",
                abs_id, spine_index,
            )
            return None
        existing_ids = _existing_ids_in_markup(id_scan_source)

        markers, item_dropped, sentence_id_to_marker_id, item_crossed = _markers_for_spine_item(
            effective_dom_map, clips, spine_index, existing_ids,
        )
        dropped_no_location += item_dropped
        crossed_inline_boundary += item_crossed
        located_clips = [c for c in clips if c.sentence_id in sentence_id_to_marker_id]
        if not located_clips:
            continue
        located_by_spine[spine_index] = located_clips
        markers_by_spine[spine_index] = markers
        marker_id_map_by_spine[spine_index] = sentence_id_to_marker_id

    if not located_by_spine:
        logger.warning(
            "🚫 Refusing to build read-along EPUB for '%s': no spine item could "
            "be anchored in the DOM",
            abs_id,
        )
        return None

    audio_bitrate = _resolve_audio_bitrate()
    source_mode = audio_bitrate == "source"
    with tempfile.TemporaryDirectory(prefix="readalong-audio-") as tmp_dir:
        transcoded_audio_path = Path(tmp_dir) / "audio.m4a"
        _safe_progress(progress_callback, "transcoding_audio", _STAGE_START["transcoding_audio"])
        transcode_span = _STAGE_START["building_overlays"] - _STAGE_START["transcoding_audio"]

        def _on_transcode_progress(local_fraction: float) -> None:
            _safe_progress(
                progress_callback, "transcoding_audio",
                _STAGE_START["transcoding_audio"] + local_fraction * transcode_span,
            )

        if not _transcode_audio_for_embed(
            source_audio_paths, audio_bitrate, transcoded_audio_path,
            progress_callback=_on_transcode_progress if progress_callback else None,
        ):
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': audio transcode failed",
                abs_id,
            )
            return None
        audio_duration = _probe_duration_seconds(transcoded_audio_path)
        if source_mode:
            # Include container overhead in the chunk-size estimate for copied VBR audio.
            audio_bitrate = (
                str(max(1, round(transcoded_audio_path.stat().st_size * 8 / audio_duration)))
                if audio_duration and audio_duration > 0 else _SOURCE_FALLBACK_AUDIO_BITRATE
            )

        if standalone_audio_output_path is not None:
            standalone_audio_output_path = Path(standalone_audio_output_path)
            standalone_audio_output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(transcoded_audio_path, standalone_audio_output_path)

        audio_archive_ext = transcoded_audio_path.suffix.lower()

        # Pass 2 (Phase 4 Part A): extend every already-located clip's end to
        # the next one's start, in book reading order across spine-item
        # boundaries, using the real probed audio duration for the very last
        # one -- see _extend_clips_to_contiguous. This step is UNCHANGED by
        # Phase 4 Part C's file splitting below: it still reasons about one
        # single, whole-book timeline, and splitting is applied strictly
        # afterward, at cut points chosen never to fall inside a clip -- see
        # the module docstring's "Phase 4 Part C" section for why that keeps
        # every file's own clips contiguous without touching this function.
        narrated_by_spine = {
            spine_index: clips for spine_index, clips in located_by_spine.items()
            if max(c.ts_end for c in clips) - min(c.ts_start for c in clips)
            >= _MIN_SECTION_NARRATION_SECONDS
        }
        if narrated_by_spine and len(narrated_by_spine) < len(located_by_spine):
            logger.info(
                "📖 Read-along for '%s': %d spine item(s) with under %.1fs of "
                "narration left without an overlay",
                abs_id, len(located_by_spine) - len(narrated_by_spine),
                _MIN_SECTION_NARRATION_SECONDS,
            )
            located_by_spine = narrated_by_spine
        unnarrated_items_skipped = 0
        unnarrated_sentences_skipped = 0
        paced_by_spine: Dict[int, List[SentenceClip]] = {}
        too_fast: List[Tuple[int, int, float, float]] = []
        for spine_index, clips in located_by_spine.items():
            item_chars = sum(c.char_end - c.char_start for c in clips)
            item_seconds = max(c.ts_end for c in clips) - min(c.ts_start for c in clips)
            item_rate = item_chars / item_seconds if item_seconds > 0 else float("inf")
            if item_rate > _MAX_SECTION_NARRATION_CHARS_PER_SECOND:
                too_fast.append((spine_index, item_chars, item_seconds, item_rate))
            else:
                paced_by_spine[spine_index] = clips
        if paced_by_spine and too_fast:
            for spine_index, item_chars, item_seconds, item_rate in too_fast:
                logger.info(
                    "⏩ Read-along for '%s': spine item %d (%s) left without an "
                    "overlay: %d chars squeezed into %.2fs (%.1f chars/s, limit "
                    "%.1f) -- text the narrator does not read",
                    abs_id, spine_index, href_by_spine.get(spine_index),
                    item_chars, item_seconds, item_rate,
                    _MAX_SECTION_NARRATION_CHARS_PER_SECOND,
                )
                unnarrated_sentences_skipped += len(located_by_spine[spine_index])
            unnarrated_items_skipped = len(too_fast)
            located_by_spine = paced_by_spine
        flat_clips = [c for clips in located_by_spine.values() for c in clips]
        flat_clips = _extend_clips_to_contiguous(flat_clips, audio_duration)
        extended_by_spine: Dict[int, List[SentenceClip]] = {}
        cursor = 0
        for spine_index, clips in located_by_spine.items():
            extended_by_spine[spine_index] = flat_clips[cursor:cursor + len(clips)]
            cursor += len(clips)

        # Phase 4 Part C: split the single transcoded file into several
        # physical audio files instead of embedding one whole-book blob.
        # `boundaries` are (start, end) ranges, in seconds, that together
        # cover [0, audio_duration) contiguously; `audio_files_on_disk` is
        # the corresponding list of (path, real_probed_duration).
        if audio_duration and audio_duration > 0:
            target_seconds = _target_audio_file_seconds(audio_bitrate)
            if source_mode:
                target_seconds = min(
                    target_seconds, _TARGET_AUDIO_FILE_BYTES * 8 / _bitrate_to_bps(audio_bitrate),
                )
            boundaries = _compute_audio_file_boundaries(flat_clips, audio_duration, target_seconds)
        else:
            boundaries = [(0.0, audio_duration or 0.0)]

        single_audio_file = len(boundaries) <= 1
        if single_audio_file:
            # No split needed (short book) or possible (duration unprobeable)
            # -- reuse the whole transcoded file exactly as Phase 4 Part B
            # produced it, with no second ffmpeg pass and no dependency on
            # `audio_duration` actually being a real number.
            audio_files_on_disk: List[Tuple[Path, float]] = [
                (transcoded_audio_path, audio_duration if audio_duration is not None else 0.0)
            ]
        else:
            split_result = _split_audio_into_files(transcoded_audio_path, boundaries, Path(tmp_dir))
            if split_result is None:
                logger.warning(
                    "🚫 Refusing to build read-along EPUB for '%s': splitting "
                    "the embedded audio into %d files failed",
                    abs_id, len(boundaries),
                )
                return None
            audio_files_on_disk = split_result

        file_starts = [start for start, _end in boundaries]
        audio_archive_paths = [
            posixpath.join(readalong_dir, f"audio{audio_archive_ext}")
            if single_audio_file
            else posixpath.join(readalong_dir, f"audio-{i}{audio_archive_ext}")
            for i in range(len(audio_files_on_disk))
        ]

        def _file_at(ts: float) -> int:
            idx = bisect.bisect_right(file_starts, ts) - 1
            return max(0, min(idx, len(audio_files_on_disk) - 1))

        # Every file belongs to one spine item (_compute_audio_file_boundaries).
        file_owner: Dict[int, int] = {}
        for c in flat_clips:
            if c.ts_end > c.ts_start:
                file_owner.setdefault(_file_at(c.ts_start), c.spine_index)

        def _file_index_for(clip: SentenceClip) -> int:
            idx = _file_at(clip.ts_start)
            # A zero-length clip exactly on a cut belongs to its own spine
            # item's file: placed at 0s of the next item's file, it would end
            # its item in the middle of that file.
            if (
                0 < idx and clip.ts_end <= file_starts[idx]
                and file_owner.get(idx - 1) == clip.spine_index
                and file_owner.get(idx) != clip.spine_index
            ):
                idx -= 1
            return idx

        # The clip that plays last in each physical file -- the only one whose
        # end may be pushed past the file (see _FILE_END_CLIP_OVERSHOOT_SECONDS).
        last_clip_by_file: Dict[int, str] = {}
        last_start_by_file: Dict[int, float] = {}
        for c in flat_clips:
            file_index = _file_index_for(c)
            if c.ts_start >= last_start_by_file.get(file_index, float("-inf")):
                last_start_by_file[file_index] = c.ts_start
                last_clip_by_file[file_index] = c.sentence_id

        modified_files: Dict[str, bytes] = {}
        new_bytes_files: Dict[str, bytes] = {}
        overlays: List[SpineOverlayResult] = []
        zero_length_clips = 0

        _safe_progress(progress_callback, "building_overlays", _STAGE_START["building_overlays"])
        for spine_index, located_clips in extended_by_spine.items():
            href = href_by_spine[spine_index]
            markers = markers_by_spine[spine_index]

            try:
                soup, nodes = injection_target_by_spine[spine_index]
                source_bytes = original_bytes_by_spine[spine_index]
                splice_stats: Dict[str, int] = {}
                modified_content = _inject_markers_into_original(soup, nodes, markers, splice_stats)
                crossed_inline_boundary += splice_stats.get("unwrapped", 0)
                _verify_marker_injection(source_bytes, modified_content, spine_index, href)
            except ValueError as e:
                logger.warning(
                    "🚫 Refusing to build read-along EPUB for '%s': marker "
                    "injection failed verification for spine item %d "
                    "(href=%s): %s",
                    abs_id, spine_index, href, e, exc_info=True,
                )
                return None
            modified_files[href] = modified_content

            chapter_id = f"c{spine_index}"
            smil_archive_path = posixpath.join(readalong_dir, f"{spine_index}.smil")
            xhtml_href_from_smil = posixpath.relpath(href, readalong_dir)
            # Finding 6 fix: the SMIL must reference whatever id was actually
            # allocated for each sentence (collision-free), not necessarily
            # its own stable SentenceClip.sentence_id -- the two differ only
            # when a collision with a pre-existing document id was found.
            id_map = marker_id_map_by_spine[spine_index]

            # Phase 4 Part C: place each clip in whichever physical audio
            # file its (already book-wide-contiguous) time range falls in,
            # and make its clip times relative to that file's own start.
            # The single-file case is left byte-for-byte as before (no
            # clamping against a possibly-meaningless probed duration when
            # there was nothing to split).
            placed_clips: List[_PlacedClip] = []
            for c in located_clips:
                file_index = _file_index_for(c)
                audio_href_from_smil = posixpath.relpath(
                    audio_archive_paths[file_index], readalong_dir,
                )
                if single_audio_file:
                    rel_start, rel_end = c.ts_start, c.ts_end
                else:
                    file_start = file_starts[file_index]
                    _file_path, file_real_duration = audio_files_on_disk[file_index]
                    rel_start = max(0.0, c.ts_start - file_start)
                    rel_end = max(rel_start, min(c.ts_end - file_start, file_real_duration))
                    if (
                        last_clip_by_file.get(file_index) == c.sentence_id
                        and rel_end >= file_real_duration - _FILE_END_CLIP_WINDOW_SECONDS
                    ):
                        rel_end = file_real_duration + _FILE_END_CLIP_OVERSHOOT_SECONDS
                placed_clips.append(_PlacedClip(
                    sentence_id=id_map[c.sentence_id],
                    ts_start=rel_start,
                    ts_end=rel_end,
                    audio_href=audio_href_from_smil,
                ))

            zero_length_clips += sum(
                1 for pc in placed_clips
                if pc.ts_end - pc.ts_start < _ZERO_LENGTH_CLIP_SECONDS
            )
            smil_bytes = _build_smil(chapter_id, xhtml_href_from_smil, placed_clips)
            new_bytes_files[smil_archive_path] = smil_bytes

            overlays.append(SpineOverlayResult(
                spine_index=spine_index,
                href=href,
                smil_href=smil_archive_path,
                par_count=len(located_clips),
                duration_seconds=sum(max(0.0, pc.ts_end - pc.ts_start) for pc in placed_clips),
            ))

        if not overlays:
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': every spine "
                "item that had sentences failed marker-injection verification "
                "(%d refused)",
                abs_id, len(located_by_spine),
            )
            return None

        audio_media_type = _audio_media_type(transcoded_audio_path)
        total_duration = sum(o.duration_seconds for o in overlays)

        existing_manifest_ids = _manifest_item_ids(opf_bytes)
        audio_manifest_entries: List[Tuple[str, str, str]] = []
        for i, archive_path in enumerate(audio_archive_paths):
            manifest_href = posixpath.relpath(archive_path, opf_dir or ".")
            base_id = "readalong-audio" if single_audio_file else f"readalong-audio-{i}"
            manifest_id = _unique_manifest_id(existing_manifest_ids, base_id)
            existing_manifest_ids.add(manifest_id)
            audio_manifest_entries.append((manifest_href, audio_media_type, manifest_id))

        try:
            modified_opf = _rewrite_opf(
                opf_bytes, opf_dir, overlays, audio_manifest_entries, total_duration,
            )
        except ValueError as e:
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': %s", abs_id, e,
                exc_info=True,
            )
            return None
        modified_files[opf_path] = modified_opf

        new_disk_files = {
            archive_path: disk_path
            for archive_path, (disk_path, _real_duration) in zip(audio_archive_paths, audio_files_on_disk)
        }
        _safe_progress(progress_callback, "packaging", _STAGE_START["packaging"])
        try:
            _package_epub(epub_path, output_path, modified_files, new_bytes_files, new_disk_files)
        except ValueError as e:
            # Finding 5: source/destination aliasing -- _package_epub already
            # refused before touching either file.
            logger.warning(
                "🚫 Refusing to build read-along EPUB for '%s': %s", abs_id, e,
                exc_info=True,
            )
            return None

    total_sentences = sum(len(clips) for clips in clips_by_spine.values())
    logger.info(
        "📖 Built read-along EPUB for '%s': %d spine overlays, %d sentences "
        "(%d dropped: no timestamp, %d dropped: no DOM location, %d spine "
        "items refused: injection verification failed, %d crossing an inline "
        "element boundary: partial highlight), "
        "%.1fs total overlay duration across %d audio file(s) (bitrate=%s) -> '%s'",
        abs_id, len(overlays), total_sentences, clip_result.dropped_no_timestamp,
        dropped_no_location, 0, crossed_inline_boundary, total_duration,
        len(audio_archive_paths), audio_bitrate, output_path,
    )
    logger.info(
        "📊 Read-along quality for '%s': %d sentences, %s interpolated, %d "
        "unnarrated pages skipped (%d sentences), %d not fully highlighted, "
        "%d zero-length clips",
        abs_id, total_sentences,
        "n/a" if clip_result.interpolated_count is None else clip_result.interpolated_count,
        unnarrated_items_skipped, unnarrated_sentences_skipped,
        crossed_inline_boundary, zero_length_clips,
    )
    return ReadalongBuildResult(
        unnarrated_spine_items_skipped=unnarrated_items_skipped,
        unnarrated_sentences_skipped=unnarrated_sentences_skipped,
        sentences_interpolated=clip_result.interpolated_count,
        zero_length_clips=zero_length_clips,
        abs_id=abs_id,
        output_path=str(output_path),
        spine_overlays=overlays,
        total_sentences=total_sentences,
        dropped_no_timestamp=clip_result.dropped_no_timestamp,
        dropped_no_location=dropped_no_location,
        total_duration_seconds=total_duration,
        audio_hrefs=[href for href, _media_type, _manifest_id in audio_manifest_entries],
        audio_bitrate=audio_bitrate,
        dropped_spine_items_injection_failed=0,
        sentences_crossing_inline_elements=crossed_inline_boundary,
    )


def _manifest_item_ids(opf_bytes: bytes) -> set:
    """The set of existing manifest item ids, read without mutating the OPF.

    Used before :func:`_rewrite_opf` to pick a collision-free id for the
    embedded audio manifest item (the per-spine SMIL ids are allocated inside
    ``_rewrite_opf`` itself, where the live, growing id set is available).
    """
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        tree = etree.fromstring(opf_bytes, parser=parser)
    except etree.XMLSyntaxError as e:
        logger.warning("Could not parse OPF to collect manifest ids: %s", e, exc_info=True)
        return set()
    manifest = tree.find(f"{{{_OPF_NS}}}manifest")
    if manifest is None:
        return set()
    return {item.get("id") for item in manifest.findall(f"{{{_OPF_NS}}}item") if item.get("id")}
