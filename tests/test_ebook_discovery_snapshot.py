"""Pure persisted-discovery snapshot classification tests."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.ebook_discovery import (
    build_discovery_snapshot,
    build_ebook_inventory,
    ebook_identity_keys,
)
from src.services.suggestions_service import SuggestionsService


def _service():
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: (_ for _ in ()).throw(AssertionError("provider read")),
        get_searchable_ebooks=lambda _query: (_ for _ in ()).throw(AssertionError("provider read")),
        audiobook_matches_search=lambda _audio, _query: False,
        get_abs_author=lambda _audio: "",
        logger=MagicMock(),
    )


def _inventory(*records):
    return build_ebook_inventory(records)


def _audio_pool(*records):
    return _service()._build_audiobook_candidate_pool(list(records))


def _audio(title, source_id, author="Author", **extra):
    return {
        "audio_source": "ABS",
        "audio_source_id": source_id,
        "audio_title": title,
        "audio_author": author,
        "audio_duration": 3600,
        **extra,
    }


def test_snapshot_classifies_empty_pool_and_audio_match_and_is_json_ready():
    service = _service()
    inventory = _inventory(
        {"source": "BookOrbit", "source_id": 1, "title": "No Match", "author": "X"},
        {"source": "BookOrbit", "source_id": 2, "title": "Matched Book", "author": "Author"},
    )
    pool = _audio_pool(_audio("Matched Book", "audio-1"))
    snapshot = build_discovery_snapshot(service, inventory, pool, now=100)

    assert snapshot["schema_version"] == 1
    assert snapshot["state"] == "complete"
    assert len(snapshot["generation"]) == 32
    assert snapshot["completed_at"] == snapshot["attempted_at"] == 100
    assert list(snapshot["audio_fingerprints_by_key"]) == ["audio-1"]
    assert snapshot["stats"]["no_audio"] == 1
    assert snapshot["stats"]["has_audio"] == 1
    matched = next(entry for entry in snapshot["entries"].values() if entry["classification"] == "has_audio")
    assert matched["audio_keys"] == ["audio-1"]
    json.dumps(snapshot)


def test_snapshot_excludes_strong_source_path_and_ebook_only_mappings():
    service = _service()
    path = "/books/one.epub"
    inventory = _inventory(
        {"source": "BookOrbit", "source_id": "mapped", "title": "Same Title", "name": "same.epub"},
        {"source": "Local File", "source_id": "local", "title": "Local", "name": "one.epub", "path": path},
        {"source": "Booklore", "source_id": "other", "title": "Same Title", "name": "same.epub", "path": "/other/same.epub"},
        {"source": "ABS", "source_id": "abs-ebook", "title": "ABS title"},
    )
    mapped_books = [
        SimpleNamespace(ebook_source="BookOrbit", ebook_source_id="mapped", ebook_only=True),
        SimpleNamespace(ebook_filename=path, original_ebook_filename=None),
        SimpleNamespace(abs_ebook_item_id="abs-ebook", ebook_source=None, ebook_source_id=None),
    ]
    snapshot = build_discovery_snapshot(service, inventory, [], mapped_books=mapped_books, now=101)

    assert snapshot["stats"]["mapped"] == 3
    assert len(snapshot["entries"]) == 1
    only_entry = next(iter(snapshot["entries"].values()))
    assert only_entry["source_id"] == "other"


def test_strong_mapping_does_not_block_other_edition_with_same_basename():
    service = _service()
    inventory = _inventory(
        {"source": "BookOrbit", "source_id": "mapped", "title": "Same Title", "name": "book.epub", "path": "/mapped/book.epub"},
        {"source": "Booklore", "source_id": "other", "title": "Same Title", "name": "book.epub", "path": "/other/book.epub"},
    )
    mapped = [SimpleNamespace(
        ebook_source="BookOrbit",
        ebook_source_id="mapped",
        ebook_filename="/mapped/book.epub",
        original_ebook_filename=None,
    )]
    snapshot = build_discovery_snapshot(service, inventory, [], mapped_books=mapped, now=102)

    assert snapshot["stats"]["mapped"] == 1
    assert len(snapshot["entries"]) == 1
    assert next(iter(snapshot["entries"].values()))["source_id"] == "other"


def test_ebook_only_kosync_filename_mapping_blocks_unhashed_local_inventory_ambiguously():
    service = _service()
    inventory = _inventory(
        {
            "source": "Local File", "source_id": "hashed", "title": "Book",
            "name": "book.epub", "path": "/library/hashed/book.epub",
            "kosync_doc_id": "known-hash",
        },
        {
            "source": "Local File", "source_id": "unhashed", "title": "Book",
            "name": "book.epub", "path": "/library/unhashed/book.epub",
        },
    )
    mapping = SimpleNamespace(
        ebook_filename="book.epub", original_ebook_filename=None,
        kosync_doc_id="known-hash", ebook_source=None, ebook_source_id=None,
        ebook_only=True,
    )
    snapshot = build_discovery_snapshot(service, inventory, [], mapped_books=[mapping], now=102)

    assert snapshot["stats"]["mapped"] == 1
    assert snapshot["stats"]["legacy_filename_ambiguous"] == 1
    assert next(iter(snapshot["entries"].values()))["classification"] == "unknown"


def test_legacy_filename_only_mapping_blocks_ambiguously_without_claiming_a_match():
    service = _service()
    inventory = _inventory(
        {"source": "BookOrbit", "source_id": "one", "title": "Book", "name": "book.epub"},
        {"source": "Booklore", "source_id": "two", "title": "Book", "name": "book.epub"},
    )
    snapshot = build_discovery_snapshot(
        service, inventory, [], mapped_books=[SimpleNamespace(
            ebook_filename="book.epub", original_ebook_filename=None,
        )], now=103,
    )

    assert snapshot["stats"]["legacy_filename_ambiguous"] == 2
    assert snapshot["stats"]["unknown"] == 2
    assert {entry["unknown_reason"] for entry in snapshot["entries"].values()} == {
        "ambiguous_legacy_filename_mapping"
    }


def test_ignored_direct_record_value_matches_cross_provider_physical_file():
    service = _service()
    current = next(iter(_inventory({
        "source": "BookOrbit", "source_id": "current", "title": "Ignored",
        "path": "/books/ignored.epub", "name": "ignored.epub",
    }).values()))
    ignored = {
        "old-key": {
            "key": "old-key",
            "source": "Grimmory",
            "source_id": "77",
            "aliases": [{"source": "Grimmory", "source_id": "77"}],
            "path": "/books/ignored.epub",
        }
    }
    snapshot = build_discovery_snapshot(service, {current["key"]: current}, [], ignored=ignored, now=104)

    assert snapshot["stats"]["ignored"] == 1
    assert snapshot["entries"] == {}


def test_file_id_identity_namespace_cannot_collide_with_provider_source_id():
    keys = ebook_identity_keys({
        "source": "BookOrbit",
        "source_id": "file:book-1",
        "source_file_id": "book-1",
    })
    assert "ebook:id:bookorbit:file%3Abook-1" in keys
    assert "ebook:file-id:bookorbit:book-1" in keys


def test_ignored_secondary_file_identity_survives_a_provider_alias_change():
    service = _service()
    current = next(iter(_inventory({
        "source": "BookOrbit", "source_id": 0, "title": "Ignore me",
        "path": "/books/ignore.epub", "name": "ignore.epub",
    }).values()))
    ignored = {
        "old-provider-key": {
            "entry": {
                "source": "Grimmory",
                "source_id": "77",
                "path": "/books/ignore.epub",
                "aliases": [{"source": "Grimmory", "source_id": "77"}],
            },
            "ignored_at": 50,
        }
    }
    assert ebook_identity_keys(current) & ebook_identity_keys(ignored["old-provider-key"]["entry"])
    snapshot = build_discovery_snapshot(service, {current["key"]: current}, [], ignored=ignored, now=104)

    assert snapshot["stats"]["ignored"] == 1
    assert snapshot["entries"] == {}


def test_incomplete_audio_search_is_unknown_not_no_audio():
    service = _service()
    inventory = _inventory({"source": "BookOrbit", "source_id": "shared", "title": "Target"})
    pool = [
        {
            "audio_source": "ABS",
            "audio_source_id": "shared",
            "bridge_key": f"audio-{index}",
            "audio_title": f"Unrelated Work {index}",
            "audio_author": "Different Author",
        }
        for index in range(501)
    ]
    snapshot = build_discovery_snapshot(service, inventory, pool, now=105)
    entry = next(iter(snapshot["entries"].values()))

    assert entry["classification"] == "unknown"
    assert entry["search_complete"] is False
    assert entry["unknown_reason"] == "candidate_search_incomplete"
    assert snapshot["stats"]["no_audio"] == 0
    reused = build_discovery_snapshot(service, inventory, pool, previous=snapshot, now=106)
    forced = build_discovery_snapshot(
        service, inventory, pool, previous=snapshot, force_refresh=True, now=107,
    )
    assert reused["stats"]["reused_cached"] == 1
    assert reused["entries"][entry["key"]]["classification"] == "unknown"
    assert forced["stats"]["scanned_new"] == 1


def test_empty_audio_pool_is_authoritative_no_audio_without_provider_calls():
    service = _service()
    inventory = _inventory({"source": "BookOrbit", "source_id": "1", "title": "Book"})
    snapshot = build_discovery_snapshot(service, inventory, [], now=106)
    assert next(iter(snapshot["entries"].values()))["classification"] == "no_audio"


def test_unchanged_snapshot_reuses_decision_without_rebuilding_index():
    service = _service()
    inventory = _inventory({"source": "BookOrbit", "source_id": "1", "title": "Book", "author": "Author"})
    pool = _audio_pool(_audio("Book", "a1"))
    previous = build_discovery_snapshot(service, inventory, pool, now=107)
    with patch("src.services.ebook_discovery.AudioCandidateIndex", side_effect=AssertionError("rebuilt")):
        current = build_discovery_snapshot(service, inventory, pool, previous=previous, now=108)

    entry = next(iter(current["entries"].values()))
    assert current["stats"]["reused_cached"] == 1
    assert current["stats"]["scanned_new"] == 0
    assert current["stats"]["candidate_count"] == 0
    assert current["stats"]["visited_count"] == 0
    assert entry["checked_at"] == 107
    assert current["generation"] != previous["generation"]


def test_changed_input_and_force_refresh_rescore_and_prune_stale_entries():
    service = _service()
    original = _inventory({"source": "BookOrbit", "source_id": "1", "title": "Book"})
    pool = _audio_pool(_audio("Book", "a1"))
    previous = build_discovery_snapshot(service, original, pool, now=109)
    changed = _inventory({"source": "BookOrbit", "source_id": "1", "title": "Renamed Book"})
    rescored = build_discovery_snapshot(service, changed, pool, previous=previous, now=110)
    audio_changed = build_discovery_snapshot(
        service, original, _audio_pool(_audio("Different", "a1")), previous=previous, now=111,
    )
    forced = build_discovery_snapshot(
        service, original, pool, previous=previous, force_refresh=True, now=112,
    )
    pruned = build_discovery_snapshot(service, {}, pool, previous=previous, now=113)

    assert rescored["stats"]["scanned_new"] == 1
    assert audio_changed["stats"]["scanned_new"] == 1
    assert forced["stats"]["scanned_new"] == 1
    assert pruned["entries"] == {}


def test_cached_local_ebook_metadata_avoids_second_metadata_read():
    from src.services import suggestions_service as module

    service = _service()
    candidate = SimpleNamespace(
        title="Filename Title",
        authors="",
        source="Local File",
        path="/library/book.epub",
        name="Filename Title.epub",
        language="en",
    )
    with patch.object(module, "read_local_ebook_metadata", return_value=("Metadata Title", "Author")):
        with patch.object(module, "read_local_ebook_series", return_value=("Series", 1.5)):
            expected = service._prepare_candidate_pool([candidate])

    candidate._local_metadata = ("Metadata Title", "Author")
    candidate._local_series = ("Series", 1.5)
    with patch.object(module, "read_local_ebook_metadata", side_effect=AssertionError("re-read")):
        with patch.object(module, "read_local_ebook_series", side_effect=AssertionError("re-read")):
            actual = service._prepare_candidate_pool([candidate])

    assert actual == expected


def test_per_user_ignore_maps_are_independent():
    service = _service()
    inventory = _inventory({"source": "BookOrbit", "source_id": "1", "title": "Book"})
    record = next(iter(inventory.values()))
    ignored = {"user-a": {"entry": record, "ignored_at": 1}}
    snapshot_a = build_discovery_snapshot(service, inventory, [], ignored=ignored, now=114)
    snapshot_b = build_discovery_snapshot(service, inventory, [], ignored={}, now=115)

    assert snapshot_a["entries"] == {}
    assert snapshot_b["stats"]["no_audio"] == 1


def test_abs_audio_identifier_does_not_merge_or_ignore_distinct_ebook_editions():
    service = _service()
    inventory = _inventory(
        {
            "source": "CWA", "source_id": "edition-one", "title": "Edition One",
            "abs_identifier": "shared-audio-id",
        },
        {
            "source": "CWA", "source_id": "edition-two", "title": "Edition Two",
            "abs_identifier": "shared-audio-id",
        },
    )
    first, second = list(inventory.values())
    assert not (ebook_identity_keys(first) & ebook_identity_keys(second))

    snapshot = build_discovery_snapshot(
        service,
        inventory,
        [],
        ignored={first["key"]: first},
        now=116,
    )

    assert snapshot["stats"]["ignored"] == 1
    assert len(snapshot["entries"]) == 1
    assert next(iter(snapshot["entries"].values()))["source_id"] == "edition-two"


def test_abs_authoritative_audio_identifier_classifies_mismatched_text_as_has_audio():
    service = _service()
    inventory = _inventory({
        "source": "CWA",
        "source_id": "ebook-edition",
        "title": "Completely Different Ebook Title",
        "author": "Different Ebook Author",
        "abs_identifier": "abs-audio-42",
    })
    pool = [{
        "audio_source": "ABS",
        "audio_source_id": "abs-audio-42",
        "bridge_key": "abs:abs-audio-42",
        "audio_title": "Unrelated Audiobook",
        "audio_author": "Another Author",
    }]

    snapshot = build_discovery_snapshot(service, inventory, pool, now=117)
    entry = next(iter(snapshot["entries"].values()))

    assert entry["classification"] == "has_audio"
    assert entry["audio_keys"] == ["abs:abs-audio-42"]
    assert entry["authoritative_match"] is True
    assert entry["search_complete"] is True


def test_non_abs_audio_with_same_id_is_not_an_authoritative_match():
    service = _service()
    inventory = _inventory({
        "source": "CWA",
        "source_id": "ebook-edition",
        "title": "Completely Different Ebook Title",
        "author": "Different Ebook Author",
        "abs_identifier": "shared-provider-id",
    })
    pool = [{
        "audio_source": "BookOrbit",
        "audio_source_id": "shared-provider-id",
        "bridge_key": "bookorbit:shared-provider-id",
        "audio_title": "Unrelated Audiobook",
        "audio_author": "Another Author",
    }]
    service.scan_ebook_candidates = lambda _ebook, shortlist: {"matches": []}

    snapshot = build_discovery_snapshot(service, inventory, pool, now=118)
    entry = next(iter(snapshot["entries"].values()))

    assert entry["classification"] == "no_audio"
    assert entry["audio_keys"] == []
    assert entry.get("authoritative_match") is not True


def test_unknown_companion_audio_is_not_a_false_no_audio_or_confirmed_pair():
    pool = _audio_pool(_audio("Possible Audio", "uncertain", author="Ada"))
    pool[0]["audio_inventory_unknown"] = True
    inventory = _inventory(
        {"source": "Kavita", "source_id": "possible", "title": "Possible Audio", "author": "Ada"},
        {"source": "Kavita", "source_id": "unrelated", "title": "Completely Different Work", "author": "Someone Else"},
    )
    snapshot = build_discovery_snapshot(_service(), inventory, pool)
    by_id = {entry["source_id"]: entry for entry in snapshot["entries"].values()}
    assert by_id["possible"]["classification"] == "unknown"
    assert by_id["possible"]["unknown_reason"] == "companion_audio_coverage_unknown"
    assert by_id["unrelated"]["classification"] == "no_audio"

    pool[0].pop("audio_inventory_unknown")
    refreshed = build_discovery_snapshot(_service(), inventory, pool, previous=snapshot)
    assert refreshed["stats"]["reused_cached"] == 0
    assert any(entry["classification"] == "has_audio" for entry in refreshed["entries"].values())
