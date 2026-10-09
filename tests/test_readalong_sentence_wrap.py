"""Marker injection wraps a sentence's WHOLE DOM range, not just its first text node.

Real-book shapes that used to highlight only the part of a sentence inside the
first text node: drop caps, small-caps openers, mid-sentence italics, dialogue
tags after an inline quote, and Calibre's empty page anchors.
"""
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from src.services.readalong_builder import (
    _inject_markers,
    _inject_markers_into_original,
    _markers_for_spine_item,
    _verify_marker_injection,
)
from src.services.readalong_segments import SentenceClip
from src.utils.ebook_dom_map import (
    SpineDomMap,
    content_string_nodes,
    joined_text,
    original_body_scope,
    parse_original_spine_xml,
    runs_from_nodes,
)

XHTML_NS = b'xmlns="http://www.w3.org/1999/xhtml"'


def _clips_for(text: str, sentences: List[str]) -> List[SentenceClip]:
    """Clips whose char ranges locate each sentence (in combined-text form,
    i.e. a single space between text runs) inside ``text``."""
    clips = []
    cursor = 0
    for n, sentence in enumerate(sentences):
        pos = text.index(sentence, cursor)
        cursor = pos + len(sentence)
        clips.append(SentenceClip(
            sentence_id=f"c1-s{n}", spine_index=1, href="ch1.xhtml",
            char_start=pos, char_end=cursor, ts_start=float(n), ts_end=float(n + 1),
        ))
    return clips


def _inject(
    original: bytes, sentences: List[str], original_path: bool = True,
    clips_override=None,
) -> Tuple[bytes, Dict[str, int], int]:
    """Run the producer + injector; returns (modified bytes, stats, crossed)."""
    if original_path:
        soup = parse_original_spine_xml(original)
        assert soup is not None
        nodes = content_string_nodes(original_body_scope(soup))
    else:
        soup = BeautifulSoup(original, "html.parser")
        nodes = content_string_nodes(soup)
    runs = runs_from_nodes(nodes)
    text = joined_text(nodes, runs)
    dom_map = [SpineDomMap(1, "ch1.xhtml", 0, len(text), runs, len(nodes), text)]
    clips = clips_override(text) if clips_override else _clips_for(text, sentences)
    markers, dropped, _ids, crossed = _markers_for_spine_item(dom_map, clips, 1, set())
    assert dropped == 0
    stats: Dict[str, int] = {}
    if original_path:
        modified = _inject_markers_into_original(soup, nodes, markers, stats)
    else:
        modified = _inject_markers(original, markers, stats)
    _verify_marker_injection(original, modified, 1, "ch1.xhtml")
    return modified, stats, crossed


def _span_text(modified: bytes, marker_id: str) -> str:
    tag = BeautifulSoup(modified, "html.parser").find(id=marker_id)
    assert tag is not None, f"{marker_id} missing from {modified!r}"
    return tag.get_text()


def _assert_unique_ids(modified: bytes) -> None:
    ids = [t["id"] for t in BeautifulSoup(modified, "html.parser").find_all(id=True)]
    assert len(ids) == len(set(ids)), ids


def _doc(body: bytes) -> bytes:
    return b"<html " + XHTML_NS + b"><head><title>t</title></head><body>" + body + b"</body></html>"


def test_drop_cap_sentence_is_fully_wrapped():
    original = _doc(b'<p><span class="dropcap">T</span>RADE season came around again.</p>')
    modified, stats, crossed = _inject(original, ["T RADE season came around again."])
    assert _span_text(modified, "c1-s0") == "TRADE season came around again."
    assert crossed == 0 and stats.get("unwrapped", 0) == 0
    _assert_unique_ids(modified)


def test_small_caps_drop_cap_with_two_leading_spans():
    original = _doc(
        b'<p><span class="dc">M</span><span class="sc">AX HELD THE</span> '
        b"door open for me as we entered the lobby.</p>"
    )
    modified, _stats, _crossed = _inject(
        original, ["M AX HELD THE door open for me as we entered the lobby."],
    )
    assert _span_text(modified, "c1-s0") == "MAX HELD THE door open for me as we entered the lobby."
    _assert_unique_ids(modified)


def test_italics_in_the_middle_of_a_sentence():
    original = _doc(
        b"<p>It wasn't <i>just</i> about the baby. It was about everything.</p>"
    )
    modified, _stats, crossed = _inject(
        original, ["It wasn't just about the baby.", "It was about everything."],
    )
    assert _span_text(modified, "c1-s0") == "It wasn't just about the baby."
    assert _span_text(modified, "c1-s1") == "It was about everything."
    assert crossed == 0
    assert b"<i>just</i>" in modified


def test_dialogue_tag_after_inline_quote():
    original = _doc(b"<p><i>\xe2\x80\x9cFor what?\xe2\x80\x9d</i> she asked. Nobody answered.</p>")
    modified, _stats, _crossed = _inject(
        original,
        ["“For what?” she asked.", "Nobody answered."],
    )
    assert _span_text(modified, "c1-s0") == "“For what?” she asked."
    assert _span_text(modified, "c1-s1") == "Nobody answered."


def test_inline_element_shared_by_two_sentences_is_split_with_unique_ids():
    original = _doc(b'<p>Hello <i id="em1" class="x">there. Now</i> go on.</p>')
    modified, stats, _crossed = _inject(original, ["Hello there.", "Now go on."])
    assert _span_text(modified, "c1-s0") == "Hello there."
    assert _span_text(modified, "c1-s1") == "Now go on."
    _assert_unique_ids(modified)
    soup = BeautifulSoup(modified, "html.parser")
    italics = soup.find_all("i")
    # sentence 0 | the inter-sentence gap | sentence 1
    assert len(italics) == 3
    assert [i.get("id") for i in italics] == ["em1", None, None]
    assert all(i.get("class") == ["x"] for i in italics)
    assert stats.get("unwrapped", 0) == 0


def test_anchor_split_across_two_sentences_keeps_id_once():
    original = _doc(b'<p>See <a id="x" href="u.xhtml">the end. Then</a> more text.</p>')
    modified, _stats, _crossed = _inject(original, ["See the end.", "Then more text."])
    assert _span_text(modified, "c1-s0") == "See the end."
    assert _span_text(modified, "c1-s1") == "Then more text."
    soup = BeautifulSoup(modified, "html.parser")
    anchors = soup.find_all("a")
    assert len(anchors) == 3
    assert [a.get("id") for a in anchors] == ["x", None, None]
    assert all(a.get("href") == "u.xhtml" for a in anchors)
    _assert_unique_ids(modified)


def test_empty_page_anchor_inside_a_sentence():
    original = _doc(b'<p>Part one <span id="page_9"/>part two ends here. Next.</p>')
    modified, _stats, crossed = _inject(original, ["Part one part two ends here.", "Next."])
    assert _span_text(modified, "c1-s0").replace("\n", "") == "Part one part two ends here."
    assert _span_text(modified, "c1-s1") == "Next."
    assert modified.count(b"page_9") == 1
    assert crossed == 0
    _assert_unique_ids(modified)


def test_prefixed_xhtml_sentence_crossing_inline_elements():
    original = (
        b'<h:html xmlns:h="http://www.w3.org/1999/xhtml"><h:body>'
        b"<h:p>Start <h:i>of one. Then</h:i> two follows.</h:p></h:body></h:html>"
    )
    modified, _stats, _crossed = _inject(original, ["Start of one.", "Then two follows."])
    assert _span_text(modified, "c1-s0") == "Start of one."
    assert _span_text(modified, "c1-s1") == "Then two follows."
    assert modified.count(b"<h:span id=") == 2
    assert modified.count(b"<h:i>") == 3
    assert b"<span" not in modified and b"<i>" not in modified


def test_fallback_html_parser_path_wraps_the_whole_sentence():
    original = b"<html><body><p><span>T</span>RADE came. Then <i>it went</i> away.</p></body></html>"
    modified, _stats, _crossed = _inject(
        original, ["T RADE came.", "Then it went away."], original_path=False,
    )
    assert _span_text(modified, "c1-s0") == "TRADE came."
    assert _span_text(modified, "c1-s1") == "Then it went away."


def test_split_without_whitespace_passes_verification():
    """An inline element cut where no whitespace exists must not trip the text check."""
    original = _doc(b"<p><i>Go.\xc2\xa0Now</i> then.</p>")
    modified, _stats, _crossed = _inject(original, ["Go.", "Now then."])
    assert _span_text(modified, "c1-s1") == "Now then."


def test_block_crossing_sentence_falls_back_and_is_counted():
    original = _doc(b"<div>Intro text<p>inside block</p></div>")

    def clips(text: str) -> List[SentenceClip]:
        return [SentenceClip("c1-s0", 1, "ch1.xhtml", 0, len(text), 0.0, 1.0)]

    modified, stats, _crossed = _inject(original, [], clips_override=clips)
    assert stats["unwrapped"] == 1
    assert _span_text(modified, "c1-s0") == "Intro text"
    assert b"<p>inside block</p>" in modified
