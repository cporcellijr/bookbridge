"""Tests for issue #383 — the same physical audiobook indexed by more than one
provider (e.g. Audiobookshelf AND Grimmory both pointed at one shared `/books`
tree) must not produce duplicate Suggestions.

Covers `SuggestionsService._physical_audio_key`, `_same_physical_audio`, the
matched-book suppression, and the within-scan collapse in
`scan_library_suggestions`. Mirrors the construction style of
`tests/test_suggestions_same_folder.py` and `tests/test_suggestions_scan_single_ebook.py`.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.services.suggestions_service import SuggestionsService


def _build_service(audiobooks=None):
    """Build a SuggestionsService with stub closures. `database_service` and
    `get_searchable_ebooks` can be reconfigured per-test after construction."""
    svc = SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: audiobooks or [],
        get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda _ab, _q: False,
        get_abs_author=lambda _ab: '',
        logger=MagicMock(),
    )
    svc.database_service.get_all_books.return_value = []
    svc.database_service.get_ignored_suggestion_source_ids.return_value = []
    return svc


def _ab(source: str, source_id: str, title: str, author: str = "", path: str = "") -> dict:
    record = {
        "audio_source": source,
        "audio_source_id": source_id,
        "audio_title": title,
        "audio_author": author,
    }
    if path:
        record["audio_path"] = path
    return record


def _ebook(title: str, authors: str = "", source: str = "Grimmory",
           source_id: str = "ebook-1", path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        name=f"{title}.epub",
        title=title,
        authors=authors,
        source=source,
        source_id=source_id,
        path=path,
    )


# ---------------------------------------------------------------------------
# 1-2. _physical_audio_key
# ---------------------------------------------------------------------------

def test_physical_audio_key_drops_equivalent_root_and_keeps_filename():
    # Pins: /audiobooks/<file> and /books/<file> must normalize to the SAME key —
    # the equivalent-library-root is dropped but the final (file) segment is kept,
    # unlike _parent_dir_key which would drop the filename too.
    svc = _build_service()
    filename = "Ali Hazelwood - Two Can Play (Kelsey Navarro Foster)).m4b"
    key_audiobooks = svc._physical_audio_key(f"/audiobooks/{filename}")
    key_books = svc._physical_audio_key(f"/books/{filename}")

    assert key_audiobooks == key_books
    assert key_audiobooks == filename.lower()


def test_physical_audio_key_keeps_full_relative_path_when_no_root_matches():
    # A non-equivalent-root path keeps every segment (lowercased), so two
    # differently-authored books under the same library never collide.
    svc = _build_service()
    key = svc._physical_audio_key("/books/Author/Series/Title/Title.m4b")
    assert key == "author/series/title/title.m4b"


def test_physical_audio_key_empty_for_blank_or_none():
    # Pins: empty/None input must never explode and must never produce a truthy key
    # that could accidentally group unrelated pathless records together.
    svc = _build_service()
    assert svc._physical_audio_key("") == ""
    assert svc._physical_audio_key(None) == ""


# ---------------------------------------------------------------------------
# 3. _same_physical_audio — pathless (BookOrbit) case
# ---------------------------------------------------------------------------

def test_same_physical_audio_false_when_either_side_is_pathless():
    # Pins requirement #1: BookOrbitAudioSourceAdapter never sets `path=`, so
    # AudioResult.path (and therefore audio_path) is "" for every BookOrbit record.
    # Two pathless records must never be treated as duplicates of each other.
    svc = _build_service()
    assert not svc._same_physical_audio("", "")
    assert not svc._same_physical_audio(None, None)
    assert not svc._same_physical_audio("", "/books/Some Book.m4b")
    assert not svc._same_physical_audio("/books/Some Book.m4b", "")


# ---------------------------------------------------------------------------
# 6-7. Distinct books must never collapse
# ---------------------------------------------------------------------------

def test_different_books_in_same_folder_are_not_collapsed():
    # Real ABS shape: two distinct single-file audiobooks under the SAME parent
    # folder. Different filenames -> different physical keys -> never collapsed.
    svc = _build_service()
    assert not svc._same_physical_audio(
        "/audiobooks/Book One.m4b",
        "/audiobooks/Book Two.m4b",
    )


def test_single_file_library_root_parent_is_not_the_dedupe_key():
    # This is the regression _parent_dir_key would have caused: for a single-file
    # ABS path, _parent_dir_key strips the filename and returns just the bare
    # library root ("audiobooks"), so it is IDENTICAL for every unrelated
    # single-file audiobook. _same_physical_audio must not inherit that collapse.
    svc = _build_service()
    assert svc._parent_dir_key("/audiobooks/Alpha Book.m4b") == "audiobooks"
    assert svc._parent_dir_key("/audiobooks/Beta Book.m4b") == "audiobooks"
    assert not svc._same_physical_audio(
        "/audiobooks/Alpha Book.m4b",
        "/audiobooks/Beta Book.m4b",
    )


# ---------------------------------------------------------------------------
# 4. Collapse within the unmatched set, provider preference
# ---------------------------------------------------------------------------

def test_duplicate_unmatched_records_collapse_to_one_preferring_abs():
    # ABS + BookLore both see the same physical file. Listed BookLore-first to
    # prove the winner is chosen by provider preference, not list/iteration order.
    svc = _build_service(audiobooks=[
        _ab("BookLore", "bl-1", "Same Book", author="Author X",
            path="/books/Same Book.m4b"),
        _ab("ABS", "abs-1", "Same Book", author="Author X",
            path="/audiobooks/Same Book.m4b"),
    ])
    svc.get_searchable_ebooks = lambda _q: [_ebook("Same Book", authors="Author X")]

    result = svc.scan_library_suggestions()

    assert result["stats"]["total_unmatched"] == 1
    assert len(result["suggestions"]) == 1
    assert result["suggestions"][0]["bridge_key"] == "abs-1"
    assert result["suggestions"][0]["audio_source"] == "ABS"


# ---------------------------------------------------------------------------
# 5. Suppression of a duplicate of an ALREADY MATCHED book
# ---------------------------------------------------------------------------

def test_duplicate_of_already_matched_book_is_suppressed():
    # The ABS copy is already linked (its bridge_key is a matched Book.abs_id).
    # The BookLore copy of the SAME physical file must not resurface as a
    # suggestion on the next scan — without this, matching one provider's copy
    # would never clear the other provider's duplicate.
    svc = _build_service(audiobooks=[
        _ab("ABS", "abs-1", "Same Book", author="Author X",
            path="/audiobooks/Same Book.m4b"),
        _ab("BookLore", "bl-1", "Same Book", author="Author X",
            path="/books/Same Book.m4b"),
    ])
    svc.database_service.get_all_books.return_value = [
        SimpleNamespace(abs_id="abs-1", audio_source=None, audio_source_id=None),
    ]
    svc.get_searchable_ebooks = lambda _q: [_ebook("Same Book", authors="Author X")]

    result = svc.scan_library_suggestions()

    assert result["stats"]["total_unmatched"] == 0
    assert result["suggestions"] == []


def test_two_pathless_bookorbit_records_are_never_collapsed_in_scan():
    # Full no-op requirement #1: BookOrbit-only installs have audio_path == "" for
    # every record, so two distinct BookOrbit audiobooks must both stay unmatched.
    svc = _build_service(audiobooks=[
        _ab("BookOrbit", "bo-1", "Book One", author="Author X"),
        _ab("BookOrbit", "bo-2", "Book Two", author="Author X"),
    ])

    result = svc.scan_library_suggestions()

    assert result["stats"]["total_unmatched"] == 2


# ---------------------------------------------------------------------------
# 8. Collapse bucketing (perf fix) must not change which records collapse
# ---------------------------------------------------------------------------

def test_collapse_bucketing_only_merges_true_duplicates_sharing_final_segment():
    # Four records all end in the same final path segment ("book.m4b"), so they
    # all land in the SAME bucket -- but only one pair (abs-1/bl-1, identical
    # physical key) is a true duplicate. bo-1 shares the final segment but has a
    # different parent ("beta" vs "alpha"), and abs-2 is a bare filename (too
    # shallow for the >=2-segment suffix rule). Pins that bucketing by final
    # segment is a pure narrowing of comparisons, not a change in which pairs
    # the union-find actually merges.
    svc = _build_service()
    records = [
        ("abs-1", _ab("ABS", "abs-1", "Alpha Book", author="Author X",
                       path="/audiobooks/Alpha/Book.m4b")),
        ("bl-1", _ab("BookLore", "bl-1", "Alpha Book", author="Author X",
                      path="/books/Alpha/Book.m4b")),
        ("bo-1", _ab("BookOrbit", "bo-1", "Beta Book", author="Author X",
                      path="/books/Beta/Book.m4b")),
        ("abs-2", _ab("ABS", "abs-2", "Bare Book", author="Author X",
                       path="Book.m4b")),
    ]

    result = svc._collapse_duplicate_physical_audio(records)

    kept_keys = [bridge_key for bridge_key, _ab in result]
    assert kept_keys == ["abs-1", "bo-1", "abs-2"]


# ---------------------------------------------------------------------------
# 9-11. Grimmory's real library roots (/Library, /Audiobook) must be
# recognized for physical-audio identity, without widening the same-folder
# ebook-matching root set.
# ---------------------------------------------------------------------------

def test_audiobookshelf_and_grimmory_library_root_are_same_physical_audio():
    # Real observed roots: ABS serves "/audiobooks/...", Grimmory serves
    # "/Library/...". Neither root matched the other before this fix (only a
    # deep-enough path bridged them via the >=2-segment suffix rule); a flat
    # single-file audiobook path is not deep enough, so this reproduces the
    # measured silent-dedupe failure.
    svc = _build_service()
    assert svc._same_physical_audio(
        "/audiobooks/Two Can Play.m4b",
        "/Library/Two Can Play.m4b",
    )


def test_grimmory_audiobook_root_and_books_root_are_same_physical_audio():
    # Real observed Grimmory audio root: "/Audiobook/..." (singular), against
    # the generic "/books/..." root another provider might use.
    svc = _build_service()
    assert svc._same_physical_audio(
        "/Audiobook/X/Y.m4b",
        "/books/X/Y.m4b",
    )


def test_paths_share_parent_unaffected_by_wider_physical_key_root_set():
    # _EQUIVALENT_LIBRARY_ROOTS (backing the same-folder EBOOK path --
    # _parent_dir_key / _same_directory_key / _paths_share_parent) must NOT
    # gain "library"/"audiobook" -- those live ONLY in
    # _PHYSICAL_KEY_LIBRARY_ROOTS for #383's physical-audio dedupe. A
    # left/right pair whose leading segments are "library" vs an unrelated,
    # non-root folder name must stay "not the same folder": dropping "library"
    # from only one side would shrink the suffix-match window and produce a
    # false positive here if _EQUIVALENT_LIBRARY_ROOTS had been edited
    # directly instead of adding the separate wider set.
    svc = _build_service()
    assert not svc._paths_share_parent(
        "/library/Author/BookA/file.epub",
        "/downloads/Author/BookA/file.epub",
    )
