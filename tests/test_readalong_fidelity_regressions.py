import pytest
from bs4 import BeautifulSoup

from src.services.readalong_builder import (
    _inject_markers_into_original,
    _resolve_spine_injection_target,
    _verify_marker_injection,
)
from src.utils.ebook_dom_map import (
    INLINE_TEXT_JOINER,
    SpineDomMap,
    content_string_nodes,
    joined_text,
    original_body_scope,
    parse_original_spine_xml,
    runs_from_nodes,
)


def _reference(content: bytes) -> tuple[SpineDomMap, str]:
    soup = BeautifulSoup(content, "html.parser")
    runs = runs_from_nodes(content_string_nodes(soup))
    text = " ".join(run.text for run in runs)
    return SpineDomMap(1, "OEBPS/ch1.xhtml", 0, len(text), runs, len(content_string_nodes(soup))), text


def test_original_mapping_preserves_body_text_and_uses_child_structure():
    canonical = b"<html><body><p>Repeat once.</p><p>Repeat twice.</p></body></html>"
    original = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="book.css"/></head>'
        b'<body data-book="keep">Leading text. <p>Repeat once.</p><p>Repeat twice.</p></body></html>'
    )
    ref, expected = _reference(canonical)

    resolved = _resolve_spine_injection_target(original, ref, expected, canonical)

    assert resolved is not None
    dom_entry, soup, nodes = resolved
    assert [run.node_index for run in dom_entry.runs] == [1, 2]
    modified = _inject_markers_into_original(
        soup, nodes, [(dom_entry.runs[0].node_index, 0, dom_entry.runs[0].node_index, len("Repeat once."), "c1-s0")]
    )
    assert b'Leading text. ' in modified
    assert b'data-book="keep"' in modified
    assert b'href="book.css"' in modified


def test_original_mapping_survives_an_inline_word_join():
    """The reference/original text-equality check inside
    ``_resolve_spine_injection_target`` used to reconstruct the "original"
    side with a blind ``" ".join(run.text ...)``, which never equals a
    combined-text slice containing ``INLINE_TEXT_JOINER`` -- silently
    refusing the higher-fidelity original-bytes injection path (falling back
    to the lossy ebooklib-reconstructed path) for any spine item with a
    bionic-reading inline word split such as ``<b>T</b>he``.
    """
    canonical = b"<html><body><p><b>T</b>he cat sat.</p></body></html>"
    original = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="book.css"/></head>'
        b'<body><p><b>T</b>he cat sat.</p></body></html>'
    )
    soup = BeautifulSoup(canonical, "html.parser")
    nodes = content_string_nodes(soup)
    runs = runs_from_nodes(nodes)
    expected = joined_text(nodes, runs)
    assert INLINE_TEXT_JOINER in expected
    ref = SpineDomMap(1, "OEBPS/ch1.xhtml", 0, len(expected), runs, len(nodes))

    resolved = _resolve_spine_injection_target(original, ref, expected, canonical)

    assert resolved is not None


def test_original_mapping_refuses_same_text_with_different_structure():
    canonical = b"<html><body><p>Repeat.</p><p>Repeat.</p></body></html>"
    original = b"<html><body><p>Repeat.</p><div><p>Repeat.</p></div></body></html>"
    ref, expected = _reference(canonical)

    assert _resolve_spine_injection_target(original, ref, expected, canonical) is None


def test_original_mapping_resolves_duplicate_text_runs_under_same_parent():
    """A real-book shape found on 'Buy a Bullet' (Gregg Hurwitz): a single
    ``<p>`` with two lone em-dash text nodes flanking an inline ``<span>``
    (a dialogue interruption, e.g. ``<p>&mdash;<span>...</span>&mdash;</p>``).

    Both dashes strip to the identical text under the identical immediate
    parent ``<p>``, so the structural-path lookup key (parent-tag ancestry +
    text) collides for both. Requiring exactly one candidate for that key
    refused the whole spine item -- and therefore the whole build -- even
    though the paragraph is completely, unambiguously matchable by document
    order. This must resolve, and the two identical runs must land on their
    own distinct, correctly-ordered original nodes rather than colliding on
    one or refusing outright.
    """
    canonical = (
        b"<html><body><p>\xe2\x80\x94<span>thank God thank God thank</span>"
        b"\xe2\x80\x94</p></body></html>"
    )
    original = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p class="tx">'
        b'\xe2\x80\x94<span class="epub-i">thank God thank God thank</span>'
        b'\xe2\x80\x94</p></body></html>'
    )
    ref, expected = _reference(canonical)

    resolved = _resolve_spine_injection_target(original, ref, expected, canonical)

    assert resolved is not None
    dom_entry, soup, nodes = resolved
    assert [run.text for run in dom_entry.runs] == [
        "—", "thank God thank God thank", "—",
    ]
    # The two identical em-dash runs must map to their own distinct,
    # document-order-increasing original nodes (the one before the <span>,
    # then the one after it) -- never the same node twice.
    node_indices = [run.node_index for run in dom_entry.runs]
    assert node_indices == sorted(node_indices)
    assert len(set(node_indices)) == len(node_indices)


def test_marker_verification_merges_wrapped_marker_split_before_text_compare():
    """A sentence-start wrap split at a non-breaking-space boundary must not
    let get_text()'s own per-string stripping-then-canonical-joining silently
    replace the internal nbsp+space gap with an ordinary single space when
    reconstructing text for comparison. The wrapped span is non-empty (it
    carries "Second sentence." -- the fixed defect's whole point is that the
    target must contain real text), so the previous decompose-if-empty
    special case cannot apply here; _verify_marker_injection's
    canonical_text() instead unwraps the (non-empty) marker span and
    re-merges the resulting adjacent text via soup.smooth() before comparing,
    which reconstructs the original node byte-for-byte, nbsp included."""
    original = b"<html><body><p>First sentence.\xc2\xa0 Second sentence.</p></body></html>"
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    wrap_start = len("First sentence.\xa0 ")
    wrap_end = wrap_start + len("Second sentence.")
    modified = _inject_markers_into_original(soup, nodes, [(0, wrap_start, 0, wrap_end, "c1-s0")])

    _verify_marker_injection(original, modified, spine_index=1, href="ch1.xhtml")


def test_marker_verification_tolerates_an_ascii_double_space_at_the_split():
    """An ORDINARY double space between two sentences must not refuse the
    spine item -- and, before the whole-book refusal above it, the entire
    book ("The Incest Nightclub", bookorbit:6051, abandoned on exactly this).

    bs4's BeautifulSoup.endData() collapses a data segment made ENTIRELY of
    BeautifulSoup.ASCII_SPACES characters to one space at PARSE time. That
    gap is never its own segment in the original markup -- it sits inside one
    larger text node with real words either side -- but marker injection
    isolates it as a lone whitespace-only node between two new spans, where
    it IS entirely ASCII whitespace and does get collapsed. The two canonical
    texts then differed by a run length that was never semantically
    significant.

    Note this corrects the defect's original description: it is ASCII
    whitespace that breaks, not non-ASCII -- a non-breaking space is not in
    ASCII_SPACES and already passed (see the nbsp test above)."""
    original = b"<html><body><p>First sentence.  Second sentence.</p></body></html>"
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    # BOTH sentences are wrapped, which is what leaves the double space as a
    # lone whitespace-only node between the two spans -- wrapping only one
    # sentence leaves the gap attached to a text node that still carries real
    # words, where bs4 never collapses it and the defect does not reproduce.
    first_end = len("First sentence.")
    second_start = len("First sentence.  ")
    second_end = second_start + len("Second sentence.")
    modified = _inject_markers_into_original(soup, nodes, [
        (0, 0, 0, first_end, "c1-s0"),
        (0, second_start, 0, second_end, "c1-s1"),
    ])
    assert b'</span>  <span' in modified, "fixture must isolate the double space"

    _verify_marker_injection(original, modified, spine_index=1, href="ch1.xhtml")


def test_marker_verification_tolerates_a_tab_at_the_split():
    """Same ASCII_SPACES collapse, via a tab rather than a double space."""
    original = b"<html><body><p>First sentence.	Second sentence.</p></body></html>"
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    first_end = len("First sentence.")
    second_start = len("First sentence.	")
    second_end = second_start + len("Second sentence.")
    modified = _inject_markers_into_original(soup, nodes, [
        (0, 0, 0, first_end, "c1-s0"),
        (0, second_start, 0, second_end, "c1-s1"),
    ])

    _verify_marker_injection(original, modified, spine_index=1, href="ch1.xhtml")


def test_marker_verification_still_catches_a_real_text_change():
    """The whitespace-run collapse must not be a blanket weakening: a genuine
    content change (a dropped word) is still refused."""
    original = b"<html><body><p>First sentence.  Second sentence here.</p></body></html>"
    corrupted = b'<html><body><p>First sentence. <span id="c1-s0">Second sentence.</span></p></body></html>'

    with pytest.raises(ValueError):
        _verify_marker_injection(original, corrupted, spine_index=1, href="ch1.xhtml")


def test_marker_verification_still_catches_a_corrupted_newline_inside_pre():
    """Independent review, finding 7: the ASCII-whitespace-run collapse that
    tolerates an ordinary double space / tab at a marker split (see the two
    tests above) must NOT also tolerate a genuinely meaningful newline
    inside <pre> (CSS `white-space: pre`) being flattened to a space --
    that is real content corruption, not an artifact of where a marker
    split happened to land. `test_marker_verification_still_catches_a_real_text_change`
    above only covers a dropped word; this covers the whitespace-specific
    hole a purely regex-based collapse would otherwise leave."""
    original = b"<html><body><pre>First line.\nSecond line.</pre></body></html>"
    corrupted = b"<html><body><pre>First line. Second line.</pre></body></html>"

    with pytest.raises(ValueError):
        _verify_marker_injection(original, corrupted, spine_index=1, href="ch1.xhtml")


def test_marker_verification_preserves_prefixed_pre_whitespace():
    """The XHTML namespace prefix must not hide a preformatted element."""
    original = (
        b'<h:html xmlns:h="http://www.w3.org/1999/xhtml"><h:body>'
        b"<h:pre>First line.\nSecond line.</h:pre></h:body></h:html>"
    )
    corrupted = original.replace(b"First line.\nSecond", b"First line. Second")

    with pytest.raises(ValueError):
        _verify_marker_injection(original, corrupted, spine_index=1, href="ch1.xhtml")


def test_marker_verification_pre_content_survives_a_legitimate_injection_elsewhere():
    """A <pre> block elsewhere in the same spine item, left completely
    untouched by marker injection, must not itself cause a false mismatch
    -- the <pre> protection has to be transparent on the happy path, not
    just strict on the corruption path above."""
    original = (
        b"<html><body><p>Hello world.</p><pre>Keep\n  this exact\tspacing.</pre></body></html>"
    )
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    modified = _inject_markers_into_original(soup, nodes, [(0, 0, 0, len("Hello world."), "c1-s0")])

    _verify_marker_injection(original, modified, spine_index=1, href="ch1.xhtml")


def test_prefixed_xhtml_gets_marker_in_the_source_namespace():
    original = (
        b'<h:html xmlns:h="http://www.w3.org/1999/xhtml"><h:body><h:p>First sentence.</h:p>'
        b'</h:body></h:html>'
    )
    soup = parse_original_spine_xml(original)
    assert soup is not None
    nodes = content_string_nodes(original_body_scope(soup))
    modified = _inject_markers_into_original(
        soup, nodes, [(0, 0, 0, len("First sentence."), "c1-s0")]
    )

    assert b"<h:span id=\"c1-s0\"" in modified
    assert b">First sentence.</h:span>" in modified
