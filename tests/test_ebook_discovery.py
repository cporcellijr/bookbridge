"""Provider-neutral ebook identity and reverse shortlist tests."""

from unittest.mock import MagicMock

import pytest

from src.services.ebook_discovery import AudioCandidateIndex, build_ebook_inventory, ebook_key
from src.services.suggestions_service import SuggestionsService


def _service(audiobooks=None):
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: audiobooks or [],
        get_searchable_ebooks=lambda _query: [],
        audiobook_matches_search=lambda _audio, _query: False,
        get_abs_author=lambda _audio: "",
        logger=MagicMock(),
    )


def _audio(title, source_id, author="J.R.R. Tolkien", path="", **extra):
    return {
        "audio_source": "BookOrbit",
        "audio_source_id": source_id,
        "bridge_key": f"bookorbit:{source_id}",
        "audio_title": title,
        "audio_author": author,
        "audio_path": path,
        **extra,
    }


def test_ebook_key_uses_canonical_source_id_and_zero():
    assert ebook_key("Grimmory", 0) == ebook_key("Booklore", "0")
    assert ebook_key("ABS", "a:b/1") == "ebook:id:abs:a%3Ab%2F1"


def test_ebook_key_uses_only_absolute_local_paths_or_reliable_document_ids():
    assert ebook_key("Local File", path="/Books/One.epub") == "ebook:path:posix%3A%2FBooks%2FOne.epub"
    assert ebook_key("Local File", path="C:\\Books\\One.epub").startswith("ebook:path:win%3A")
    assert ebook_key("", path="C:\\Books\\One.epub") == ebook_key("", path="c:/books/one.epub")
    assert ebook_key("", path="/Books/One.epub") != ebook_key("", path="/books/One.epub")
    assert ebook_key("", path="/books/Shared") is None
    assert ebook_key("", path="/books/One.kepub") is not None
    assert ebook_key("", path="https://example.test/One.epub") is None
    assert ebook_key("", path="relative/One.epub") is None
    assert ebook_key("", document_id="urn:isbn:9780306406157").startswith("ebook:document:")
    assert ebook_key("", document_id="not-a-stable-id") is None


def test_inventory_merges_only_exact_physical_files_and_keeps_provider_provenance():
    first = {
        "source": "BookOrbit",
        "source_id": 0,
        "title": "Same Title",
        "name": "same.epub",
        "path": "/books/series/same.epub",
        "author": "Author",
        "seriesName": "Series",
        "seriesIndex": "1/2",
    }
    initial = build_ebook_inventory([first])
    initial_key = next(iter(initial))
    second = {
        "source": "Grimmory",
        "source_id": "77",
        "title": "Same Title",
        "name": "same.epub",
        "path": "/books/series/same.epub",
        "author": "Author",
    }
    merged = build_ebook_inventory([first, second], previous_entries=initial)
    reversed_merge = build_ebook_inventory([second, first], previous_entries=initial)
    provider_absent = build_ebook_inventory([second], previous_entries=initial)
    assert list(merged) == [initial_key]
    assert list(reversed_merge) == [initial_key]
    assert list(provider_absent) == [initial_key]
    record = merged[initial_key]
    assert record["series_name"] == "Series"
    assert record["series_sequence"] == 0.5
    assert record["provenance"] == ["BookOrbit", "Booklore"]
    assert record["aliases"] == [
        {"source": "BookOrbit", "source_id": "0"},
        {"source": "Booklore", "source_id": "77"},
    ]


def test_inventory_does_not_merge_directory_paths_or_same_id_different_files():
    same_directory = [
        {"source": "BookOrbit", "source_id": "one", "path": "/books/Shared", "filename": "one.epub"},
        {"source": "Booklore", "source_id": "two", "path": "/books/Shared", "filename": "two.epub"},
    ]
    assert len(build_ebook_inventory(same_directory)) == 2

    same_id = [
        {"source": "BookOrbit", "source_id": "same", "path": "/books/a/book.epub"},
        {"source": "BookOrbit", "source_id": "same", "path": "/books/b/book.epub"},
    ]
    assert len(build_ebook_inventory(same_id)) == 2


def test_inventory_retains_file_and_document_identity_metadata_without_using_isbn_as_key():
    item = {
        "source": "BookOrbit",
        "source_id": "book",
        "sourceFileId": "file-1",
        "ebookFileId": "file-2",
        "abs_identifier": "abs-1",
        "kosync_doc_id": "doc-1",
        "document_id": "urn:Library:CaseSensitivePayload",
        "isbn": "9780306406157",
        "doi": "10.1000/example",
        "path": "/books/book.epub",
    }
    record = next(iter(build_ebook_inventory([item]).values()))
    assert record["source_file_id"] == "file-1"
    assert record["ebook_file_id"] == "file-2"
    assert record["abs_identifier"] == "abs-1"
    assert record["kosync_doc_id"] == "doc-1"
    assert record["document_id"] == "urn:Library:CaseSensitivePayload"
    assert record["isbn"] == "9780306406157"
    assert record["doi"] == "10.1000/example"
    assert ebook_key("", document_id="urn:Library:CaseSensitivePayload") != ebook_key(
        "", document_id="urn:Library:casesensitivepayload"
    )
    assert ebook_key("", document_id="isbn:9780306406157") is None


def test_inventory_keeps_same_title_and_filename_in_separate_folders():
    candidates = [
        {"source": "Local File", "title": "Same", "name": "same.epub", "path": "/a/same.epub"},
        {"source": "Local File", "title": "Same", "name": "same.epub", "path": "/b/same.epub"},
    ]
    assert len(build_ebook_inventory(candidates)) == 2


def test_inventory_skips_candidates_without_reliable_identity():
    inventory = build_ebook_inventory([
        {"source": "Local File", "title": "Title", "name": "book.epub"},
    ])
    assert inventory == {}


def test_audio_pool_accepts_loaded_candidates_without_relisting():
    service = _service()
    service.get_audiobooks_conditionally = MagicMock(side_effect=AssertionError("relisted"))
    pool = service._build_audiobook_candidate_pool([
        {"audio_source": "BookOrbit", "audio_source_id": "1", "audio_title": "Book"},
    ])
    assert pool[0]["audio_title"] == "Book"
    service.get_audiobooks_conditionally.assert_not_called()


@pytest.mark.parametrize(
    ("ebook_author", "audio_author"),
    [
        ("M. A. Bulgakov", "Mikhail Afanasyevich Bulgakov"),
        ("Bulgakov", "Mikhail Afanasyevich Bulgakov"),
    ],
)
def test_index_reverse_scan_keeps_surname_initial_and_bare_surname_matches(
    ebook_author, audio_author
):
    service = _service()
    pool = [_audio("Heart of a Dog", "bulgakov", audio_author)]
    index = AudioCandidateIndex(service, pool)
    result = service.scan_ebook_candidates({
        "source": "Booklore",
        "source_id": "ebook-bulgakov",
        "title": "The Heart of the Dog",
        "author": ebook_author,
    }, index)

    assert result is not None
    assert result["complete"] is True
    assert result["matches"][0]["audio_source_id"] == "bulgakov"
    assert result["matches"][0]["score"] >= 60


def test_index_shortlists_provider_neutral_reverse_match():
    service = _service()
    pool = [
        _audio("The Lord of the Rings", "a1"),
        _audio("Pride and Prejudice", "a2", "Jane Austen"),
    ]
    index = AudioCandidateIndex(service, pool)
    result = service.scan_ebook_candidates({
        "source": "Booklore",
        "source_id": "ebook-1",
        "title": "Lord of the Rings",
        "authors": "Tolkien",
        "filename": "lotr.epub",
    }, index)

    assert result["complete"] is True
    assert result["matches"][0]["bridge_key"] == "bookorbit:a1"
    assert result["ebook_anchor"]["source"] == "Booklore"
    assert result["ebook_anchor"]["source_id"] == "ebook-1"
    assert result["ebook_anchor"]["grimmory_id"] == "ebook-1"


def test_one_component_library_parent_preserves_same_folder_candidate():
    service = _service()
    index = AudioCandidateIndex(service, [
        _audio("Completely Different Work", "a1", "Different Author", "/books/Shared/a.m4b"),
    ])
    result = service.scan_ebook_candidates({
        "title": "Warbreaker",
        "author": "Another Author",
        "path": "/books/Shared/book.epub",
    }, index)
    assert result is not None
    assert result["matches"][0]["match_reason"] == "same_folder_ambiguous"
    assert result["matches"][0]["score"] == 94.0


def test_sparse_title_without_common_bigrams_is_not_falsely_declared_absent():
    service = _service()
    ebook_title = "aXbcYdeZfW"
    audio_title = "abUcVdRefS"
    index = AudioCandidateIndex(service, [_audio(audio_title, "sparse", "Author")])
    result = index.shortlist({"title": ebook_title, "author": "Author"})
    assert result["unknown"] or any(item["audio_source_id"] == "sparse" for item in result["candidates"])


def test_volume_mismatch_and_same_folder_ambiguity_remain_reviewable():
    service = _service()
    volume_index = AudioCandidateIndex(service, [
        _audio("The Daughters' War, Book 2", "wrong-volume", "Christopher Buehlman"),
    ])
    assert service.scan_ebook_candidates({
        "title": "The Daughters' War, Book 1",
        "author": "Christopher Buehlman",
    }, volume_index) is None

    shared_folder = "/books/Author/Shared"
    folder_index = AudioCandidateIndex(service, [
        _audio("Warbreaker", "one", "Different Author", path=shared_folder + "/one.m4b"),
        _audio("Mistborn", "two", "Different Author", path=shared_folder + "/two.m4b"),
    ])
    result = service.scan_ebook_candidates({
        "title": "Warbreaker",
        "author": "Another Author",
        "path": shared_folder + "/warbreaker.epub",
    }, folder_index)
    assert result is not None
    assert len(result["matches"]) == 2
    assert {match["score"] for match in result["matches"]} == {94.0}
    assert {match["match_reason"] for match in result["matches"]} == {"same_folder_ambiguous"}


def test_budget_exhaustion_surfaces_unknown_in_reverse_scan():
    service = _service()
    index = AudioCandidateIndex(
        service,
        [_audio("Some Different Book", "a1")],
        visit_budget=0,
    )
    result = service.scan_ebook_candidates(
        {"title": "A Particular Title", "author": "Someone"},
        index,
    )
    assert result is not None
    assert result["unknown"] is True
    assert result["complete"] is False


def test_author_index_bounds_known_author_queries_and_memoizes_repeats():
    service = _service()
    pool = [
        _audio(f"Unrelated Novel {i}", str(i), f"Writer Person{i:03}")
        for i in range(250)
    ]
    pool.append(_audio("The Hidden Archive", "match", "J.R.R. Tolkien"))
    index = AudioCandidateIndex(service, pool, visit_budget=500)
    anchor = {"title": "The Hidden Archive", "author": "Tolkien"}
    first = index.shortlist(anchor)
    repeated = index.shortlist({"title": "Archive Hidden", "author": "Tolkien"})
    no_match = index.shortlist({
        "title": "A Completely Different Work",
        "author": "Unrelated Distinguished Author",
    })

    assert first["complete"] and first["candidate_count"] < 10
    assert first["visited_count"] < len(pool)
    assert repeated["author_cache_hit"] is True
    assert no_match["complete"] is True
    assert no_match["candidates"] == []
    assert no_match["visited_count"] < 500


def test_index_limits_scoring_for_a_large_synthetic_library():
    service = _service()
    pool = [_audio(f"Completely Different Volume {i}", str(i), f"Author {i}") for i in range(1000)]
    pool.append(_audio("The Lord of the Rings", "match", "J.R.R. Tolkien"))
    index = AudioCandidateIndex(service, pool)
    ebook = {"title": "Lord of the Rings", "author": "Tolkien"}
    shortlist = index.shortlist(ebook)

    calls = 0
    original = service._ebook_audio_pair_score

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    service._ebook_audio_pair_score = counted
    result = service.scan_ebook_candidates(ebook, index)
    assert result["candidate_count"] < len(pool)
    assert result["visited_count"] < 50000
    assert calls < len(pool)


def test_inventory_and_index_do_not_call_provider_operations():
    service = _service()
    service.get_searchable_ebooks = MagicMock(side_effect=AssertionError("provider call"))
    service.get_audiobooks_conditionally = MagicMock(side_effect=AssertionError("provider call"))
    candidates = [{"source": "BookOrbit", "source_id": 1, "title": "Book"}]
    inventory = build_ebook_inventory(candidates)
    index = AudioCandidateIndex(service, [_audio("Book", "a1")])
    assert inventory
    assert index.shortlist({"title": "Book"})["candidates"]
    service.get_searchable_ebooks.assert_not_called()
    service.get_audiobooks_conditionally.assert_not_called()
