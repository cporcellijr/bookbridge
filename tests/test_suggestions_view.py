"""Pure Suggestions projection, filtering and selection tests."""

from types import SimpleNamespace

import pytest

from src.services.suggestions_view import (
    project_suggestions,
    resolve_pair_selection,
)
from src.web_server import _suggestion_bulk_block


def _match(
    filename="Book.epub",
    *,
    score=100,
    author_agrees=True,
    reason="exact",
    title_key="book",
    edition_credit=False,
):
    return {
        "source": "Kavita",
        "source_id": filename,
        "ebook_filename": filename,
        "display_name": filename.removesuffix(".epub"),
        "score": score,
        "match_reason": reason,
        "author_agrees": author_agrees,
        "title_key": title_key,
        "edition_credit": edition_credit,
        "evidence": ["same title", "same author"],
    }


def _pair(key, title, *, series="River Road", sequence=None, matches=None, source="ABS"):
    return {
        "bridge_key": key,
        "audio_source": source,
        "audio_source_id": key,
        "audio_title": title,
        "audio_author": "Ada Writer",
        "audio_path": f"/audio/{title}.m4b",
        "series_name": series,
        "series_key": series.lower() if series else None,
        "series_sequence": sequence,
        "matches": matches if matches is not None else [_match(f"{title}.epub")],
    }


def _project_pairs(pairs, **kwargs):
    return project_suggestions(
        scan_cache_payload={
            "scan_cache_by_abs": {item["bridge_key"]: item for item in pairs},
            "scan_generation": "pair-generation-1",
            "scanned_at": 1730000000,
        },
        bulk_block=_suggestion_bulk_block,
        **kwargs,
    )


def test_pair_projection_preserves_evidence_and_uses_live_mapped_ignored_queue_status():
    eligible = _pair("abs-eligible", "Quiet Book")
    tie = _pair("abs-tie", "Tied Book", matches=[
        _match("one.epub"), _match("two.epub"),
    ])
    edition = _pair("abs-edition", "Edition Book", matches=[
        _match("edition.epub", edition_credit=True),
    ])
    author = _pair("abs-author", "Unconfirmed Book", matches=[
        _match("author.epub", author_agrees=False),
    ])
    mapped = _pair("abs-mapped", "Mapped Book")
    ignored = _pair("abs-ignored", "Ignored Book")
    original_matches = eligible["matches"]

    result = _project_pairs(
        [eligible, tie, edition, author, mapped, ignored],
        mapped_books=[SimpleNamespace(abs_id="abs-mapped")],
        queue=[{"bridge_key": "abs-eligible"}],
        ignored_pair_keys={"abs-ignored"},
    )

    by_key = {entry["bridge_key"]: entry for entry in result["entries"]}
    assert set(by_key) == {"abs-eligible", "abs-tie", "abs-edition", "abs-author"}
    assert by_key["abs-eligible"]["queued"] is True
    assert by_key["abs-eligible"]["bulk_eligible"] is False
    assert by_key["abs-eligible"]["bulk_block_reason"] == "already_queued"
    assert by_key["abs-tie"]["bulk_block_reason"] == "tie"
    assert by_key["abs-edition"]["bulk_block_reason"] == "edition"
    assert by_key["abs-author"]["bulk_block_reason"] == "author_unconfirmed"
    assert by_key["abs-eligible"]["matches"] == original_matches
    by_key["abs-eligible"]["matches"][0]["evidence"].append("consumer mutation")
    assert "consumer mutation" not in original_matches[0]["evidence"]
    assert result["counts"] == {
        "total": 4, "active": 4, "filtered": 4, "eligible": 0, "queued": 1,
        "manual_review": 3, "ignored": 0,
    }


def test_pair_projection_filters_exact_source_series_and_candidate_search_then_sorts_positions():
    pairs = [
        _pair("first", "Zulu", sequence=1, series="River Road"),
        _pair("zero", "Middle", sequence=0, series="River Road"),
        _pair("half", "Alpha", sequence=0.5, series="River Road"),
        _pair("unsequenced", "After", sequence=None, series="River Road"),
        _pair("near", "Another", sequence=0, series="River Roads", source="BookOrbit"),
    ]
    result = _project_pairs(pairs)
    assert [item["bridge_key"] for item in result["entries"][:4]] == [
        "zero", "half", "first", "unsequenced"
    ]
    assert result["series"][0]["series_name"] == "River Road"
    assert {series["series_key"] for series in result["series"]} == {
        "river road", "river roads"
    }

    filtered = _project_pairs(
        pairs, filters={"q": "another", "source": "bookorbit", "series_key": "RIVER ROADS"}
    )
    assert [item["bridge_key"] for item in filtered["entries"]] == ["near"]
    assert filtered["counts"]["total"] == 5
    assert filtered["counts"]["filtered"] == 1

    candidate_query = _project_pairs(
        [_pair("candidate", "Anchor", matches=[_match("Specific Candidate.epub")])],
        filters={"q": "specific candidate"},
    )
    assert len(candidate_query["entries"]) == 1


def test_legacy_pair_cache_uses_stable_generation_from_scanned_at():
    payload = {
        "scan_cache_by_abs": {"one": _pair("one", "Alpha")},
        "scanned_at": 1730000000,
    }
    first = project_suggestions(
        scan_cache_payload=payload, bulk_block=_suggestion_bulk_block
    )
    second = project_suggestions(
        scan_cache_payload=payload, bulk_block=_suggestion_bulk_block
    )
    assert first["generation"] == second["generation"] == "legacy:1730000000"
    resolved = resolve_pair_selection(
        first,
        {"generation": "legacy:1730000000", "all_filtered": True},
        current_generation=second["generation"],
    )
    assert resolved["selected_bridge_keys"] == ["one"]


def test_pair_sort_confidence_and_never_scanned_state():
    payload = {
        "scan_cache_by_abs": {
            "low": _pair("low", "Low", matches=[_match("low.epub", score=80)]),
            "high": _pair("high", "High", matches=[_match("high.epub", score=100)]),
        },
        "scan_generation": "g1",
    }
    result = project_suggestions(
        scan_cache_payload=payload,
        bulk_block=_suggestion_bulk_block,
        sort="confidence",
    )
    assert [entry["bridge_key"] for entry in result["entries"]] == ["high", "low"]
    assert result["scan"]["state"] == "complete"
    assert project_suggestions(tab="ebook_only")["scan"]["state"] == "never_scanned"


def test_ebook_only_uses_classification_identity_mapping_and_persisted_ignore_rows():
    snapshot = {
        "generation": "ebook-generation",
        "state": "failed",
        "completed_at": 100,
        "attempted_at": 200,
        "source_states": {"Kavita": {"complete": False}},
        "stats": {"inventory_total": 5},
        "error": "provider unavailable",
        "entries": {
            "kavita:no-audio": {
                "key": "kavita:no-audio", "title": "Unlinked", "author": "Ada",
                "filename": "Unlinked.epub", "source": "Kavita",
                "source_id": "no-audio", "provenance": ["Kavita"],
                "series_name": "River Road", "series_sequence": 0.5,
                "classification": "no_audio",
            },
            "kavita:has-audio": {
                "key": "kavita:has-audio", "title": "Already paired",
                "source": "Kavita", "source_id": "has-audio",
                "classification": "has_audio",
            },
            "kavita:unknown": {
                "key": "kavita:unknown", "title": "Unknown",
                "source": "Kavita", "source_id": "unknown",
                "classification": "unknown",
            },
            "kavita:mapped": {
                "key": "kavita:mapped", "title": "Mapped",
                "source": "Kavita", "source_id": "mapped",
                "classification": "no_audio",
            },
            "kavita:ignored": {
                "key": "kavita:ignored", "title": "Ignored inventory row",
                "source": "Kavita", "source_id": "ignored",
                "classification": "no_audio",
            },
        },
    }
    ignored = {
        "kavita:ignored": {
            "entry": {
                "key": "kavita:ignored", "title": "Retained ignored",
                "filename": "Retained.epub", "source": "Kavita",
                "source_id": "ignored", "provenance": ["Kavita"],
                "series_name": "River Road", "series_sequence": 0,
            },
            "ignored_at": 300,
        }
    }
    result = project_suggestions(
        tab="ebook_only",
        ebook_snapshot=snapshot,
        mapped_books=[SimpleNamespace(ebook_source="Kavita", ebook_source_id="mapped")],
        ebook_ignored=ignored,
        filters={"source": "kavita", "series_key": "river road", "ignored": "include"},
    )
    assert result["scan"]["state"] == "failed"
    assert result["scan"]["completed_at"] == 100
    assert result["scan"]["attempted_at"] == 200
    assert result["scan"]["error"] == "provider unavailable"
    assert {entry["ebook_key"] for entry in result["entries"]} == {
        "kavita:no-audio", "kavita:ignored",
    }
    assert all(not entry["bulk_eligible"] for entry in result["entries"])
    assert result["counts"]["ignored"] == 1

    ignored_only = project_suggestions(
        tab="ebook_only", ebook_snapshot=snapshot, ebook_ignored=ignored,
        filters={"ignored": "only"},
    )
    assert [entry["title"] for entry in ignored_only["entries"]] == ["Retained ignored"]


def test_per_user_payloads_are_copied_and_filters_do_not_leak():
    one = _project_pairs([_pair("one", "Alpha")], filters={"q": "alpha"})
    two = _project_pairs([_pair("two", "Beta")], filters={"q": "beta"})
    one["entries"][0]["title"] = "changed"
    assert two["entries"][0]["title"] == "Beta"
    assert one["generation"] == two["generation"] == "pair-generation-1"


def test_selection_resolves_all_eligible_series_children_and_reports_skips():
    pairs = [
        _pair(f"abs-{index}", f"Book {index}", series="The Long Series", sequence=index)
        for index in range(150)
    ]
    pairs.append(_pair("blocked", "Blocked", series="The Long Series", matches=[
        _match("blocked.epub", author_agrees=False),
    ]))
    view = _project_pairs(pairs)
    selection = resolve_pair_selection(
        view,
        {"generation": "pair-generation-1", "series_keys": ["THE LONG SERIES"]},
        current_generation="pair-generation-1",
    )
    assert len(selection["selected_keys"]) == 150
    assert selection["skipped"] == [{"key": "pair:blocked", "reason": "author_unconfirmed"}]
    assert selection["counts"] == {"selected": 150, "skipped": 1}

    all_filtered = resolve_pair_selection(
        view,
        {"generation": "pair-generation-1", "all_filtered": True},
        current_generation="pair-generation-1",
    )
    assert len(all_filtered["selected_bridge_keys"]) == 150
    empty = resolve_pair_selection(
        view,
        {"generation": "pair-generation-1"},
        current_generation="pair-generation-1",
    )
    assert empty["selected"] == []


def test_selection_stale_generation_and_bad_filters_fail_closed():
    view = _project_pairs([_pair("one", "Alpha")])
    with pytest.raises(ValueError, match="stale_generation"):
        resolve_pair_selection(
            view,
            {"generation": "old", "all_filtered": True},
            current_generation="pair-generation-1",
        )
    with pytest.raises(ValueError, match="invalid_status"):
        project_suggestions(
            bulk_block=_suggestion_bulk_block,
            filters={"status": "everything"},
        )
    with pytest.raises(ValueError, match="unsupported_filter"):
        project_suggestions(
            tab="ebook_only", filters={"status": "eligible"},
        )
    with pytest.raises(ValueError, match="unsupported_sort"):
        project_suggestions(tab="ebook_only", sort="confidence")
    with pytest.raises(ValueError, match="invalid_selection"):
        resolve_pair_selection(
            view,
            {"generation": "pair-generation-1", "count": 1},
            current_generation="pair-generation-1",
        )


def test_zero_timestamp_is_never_scanned_and_incomplete_attempt_keeps_pair_generation():
    empty = project_suggestions(
        scan_cache_payload={"scanned_at": 0.0, "scan_cache_by_abs": {}},
        bulk_block=_suggestion_bulk_block,
    )
    assert empty["scan"]["state"] == "never_scanned"
    assert empty["generation"] is None
    stale = project_suggestions(
        scan_cache_payload={
            "scanned_at": 100, "scan_generation": "previous",
            "ebook_discovery": {"state": "incomplete", "attempted_at": 200,
                                "error": "source_incomplete"},
        }, bulk_block=_suggestion_bulk_block,
    )
    assert stale["generation"] == "previous"
    assert stale["scan"]["state"] == "incomplete"
    assert stale["scan"]["completed_at"] == 100
    assert stale["scan"]["attempted_at"] == 200


def test_ungrouped_stale_keys_stay_ungrouped_and_fractional_positions_are_numeric():
    ungrouped = _pair("none", "No Series", series=None)
    ungrouped["series_key"] = "stale"
    half = _pair("half", "Half", sequence="1/2")
    view = _project_pairs([ungrouped, half])
    by_key = {row["bridge_key"]: row for row in view["entries"]}
    assert by_key["none"]["series_key"] is None
    assert by_key["half"]["series_sequence"] == 0.5
    assert [group["series_key"] for group in view["series"]] == ["river road"]


def test_group_selection_obeys_filter_and_skips_queued_and_manual_children():
    records = [_pair("include", "Keep This"), _pair("hidden", "Different"),
               _pair("queued", "Keep Queued"), _pair("manual", "Keep Manual",
               matches=[_match("a.epub"), _match("b.epub")])]
    view = _project_pairs(records, filters={"q": "keep"}, queue=[{"bridge_key": "queued"}])
    result = resolve_pair_selection(view, {
        "generation": view["generation"], "series_keys": ["River Road"],
    }, current_generation=view["generation"])
    assert result["selected_bridge_keys"] == ["include"]
    assert {row["reason"] for row in result["skipped"]} == {"tie", "already_queued"}
