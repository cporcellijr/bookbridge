"""Every path that creates an audiobook mapping must fold in a duplicate.

Two mappings for one ebook is never right: KOSync names a document by its content
hash and ``KosyncDocument.linked_abs_id`` holds exactly one book, so the loser of
such a pair is listed and served to devices but can never receive progress, and
the device downloads a second copy of bytes it already has.

This kept happening because the merge was implemented per-path. It lived inline in
the ABS branch of ``match()`` and nowhere else, so BookOrbit matches from the book
page and Suggestions auto-matches each silently duplicated. Rather than fix the
paths one at a time as each one is discovered in production, this test fails when a
*new* mapping-creating function appears without a merge.
"""

import ast
import os
import sys
from pathlib import Path

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO = Path(__file__).resolve().parents[1]
SOURCES = (
    REPO / "src" / "web_server.py",
    REPO / "src" / "services" / "book_mapping_service.py",
)
# Either the shared helper, or the older inline sequence that match() still uses.
MERGE_MARKERS = ("absorb_duplicate_mapping", "migrate_book_data")


def _creates_an_audiobook_mapping(segment: str) -> bool:
    """A function that stamps sync_mode audiobook is minting a mapping row."""
    return ('sync_mode = "audiobook"' in segment
            or "sync_mode = 'audiobook'" in segment
            or 'sync_mode="audiobook"' in segment
            or "sync_mode='audiobook'" in segment)


def test_every_audiobook_mapping_path_merges_duplicates():
    offenders = []
    checked = []

    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = ast.get_source_segment(source, node) or ""
            if not _creates_an_audiobook_mapping(segment):
                continue
            checked.append(f"{path.name}:{node.name}")
            if not any(marker in segment for marker in MERGE_MARKERS):
                offenders.append(f"{path.name}:{node.name} (line {node.lineno})")

    # match() stamps sync_mode from a variable, so the literal detector above does
    # not see it. It is the original home of the merge and must keep one, so it is
    # pinned by name rather than discovered.
    web_server_src = (REPO / "src" / "web_server.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(web_server_src)):
        if isinstance(node, ast.FunctionDef) and node.name == "match":
            segment = ast.get_source_segment(web_server_src, node) or ""
            checked.append("web_server.py:match")
            if not any(marker in segment for marker in MERGE_MARKERS):
                offenders.append("web_server.py:match (line %d)" % node.lineno)
            break
    else:
        raise AssertionError("web_server.match() not found -- this pin has drifted")

    assert checked, "found no mapping-creating functions -- the detector has drifted"
    assert not offenders, (
        "these create an audiobook mapping without folding in an existing mapping "
        "for the same ebook; call database_service.absorb_duplicate_mapping "
        "after save_book: " + ", ".join(offenders)
    )
