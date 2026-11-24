"""BookOrbit edition links (an ebook and an audiobook grouped as editions of one book)
are confirmed Discover matches: 100, `authoritative`, labelled by `authority`."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.web_server as ws
from src.api.bookorbit_client import BookOrbitClient
from src.services.ebook_discovery import build_discovery_snapshot, build_ebook_inventory
from src.services.suggestions_service import SuggestionsService
from src.web_server import _auto_match_suggestions

LINKED_CARD = {
    "id": 2348,
    "title": "Shared Book",
    "authors": [{"name": "Author"}],
    "files": [{"id": 11, "format": "epub", "role": "primary"}],
    "linked": {
        "groupId": 7,
        "books": [
            {"bookId": 5114, "libraryId": 2, "formats": ["m4b"], "mediaKind": "audiobook"},
            {"bookId": 9001, "libraryId": 1, "formats": ["epub"], "mediaKind": "ebook"},
        ],
        "memberCount": 3,
    },
}


def _service() -> SuggestionsService:
    return SuggestionsService(
        database_service=MagicMock(),
        container=MagicMock(),
        manager=MagicMock(),
        get_audiobooks_conditionally=lambda: [],
        get_searchable_ebooks=lambda _q: [],
        audiobook_matches_search=lambda _ab, _q: False,
        get_abs_author=lambda _ab: "",
        logger=MagicMock(),
    )


def _ebook(source_id, title="Different Ebook Title", edition_audio_ids=(), language="", source="BookOrbit"):
    return SimpleNamespace(
        name=f"{title}-{source_id}.epub", title=title, authors="Someone Else", source=source,
        source_id=source_id, path=None, language=language, edition_audio_ids=list(edition_audio_ids),
    )


def _audio(**extra):
    return {"audio_source": "BookOrbit", "audio_source_id": "5114", "bridge_key": "bookorbit:5114",
            "audio_title": "Unrelated Audio Title", "audio_author": "Narrated Author", **extra}


@pytest.fixture
def client():
    with patch.dict(os.environ, {"BOOKORBIT_SERVER": "http://mock", "BOOKORBIT_USER": "u",
                                 "BOOKORBIT_PASSWORD": "p"}):
        yield BookOrbitClient()


def test_client_keeps_audiobook_editions_from_the_card(client):
    unlinked = {key: value for key, value in LINKED_CARD.items() if key != "linked"}
    unlinked["id"] = 1
    response = MagicMock(status_code=201)
    response.json.return_value = {"items": [LINKED_CARD, unlinked], "total": 2}
    with patch.object(client, "_make_request", return_value=response):
        assert client._refresh_book_cache() is True

    by_id = {book["id"]: book for book in client.get_all_ebooks()}
    assert by_id[2348]["editionAudioIds"] == ["5114"]
    assert by_id[1]["editionAudioIds"] == []


def test_client_counts_a_member_with_audio_files_even_when_its_primary_is_an_ebook():
    card = {"linked": {"books": [{"bookId": 8, "formats": ["epub", "m4b"], "mediaKind": "ebook"}]}}
    assert BookOrbitClient._edition_audio_ids(card) == ["8"]
    assert BookOrbitClient._edition_audio_ids({"linked": None}) == []


def test_bookorbit_catalog_carries_edition_ids_to_the_ebook_result(tmp_path):
    clients = SimpleNamespace(
        booklore_client=MagicMock(is_configured=MagicMock(return_value=False)),
        bookorbit_client=MagicMock(is_configured=MagicMock(return_value=True), get_all_ebooks=MagicMock(
            return_value=[{"id": 2348, "title": "Shared Book", "authors": "Author",
                           "fileName": "Shared Book.epub", "editionAudioIds": ["5114"]}])),
        bookfusion_client=MagicMock(is_configured=MagicMock(return_value=False)),
        kavita_client=MagicMock(is_configured=MagicMock(return_value=False)),
        abs_client=MagicMock(search_ebooks=MagicMock(return_value=[])),
        library_service=None,
    )
    with patch.object(ws, "EBOOK_DIR", tmp_path, create=True), patch.object(ws, "uc", return_value=clients):
        results = ws.get_searchable_ebooks("")

    assert [(r.source_id, r.edition_audio_ids) for r in results] == [(2348, ["5114"])]


def test_linked_pair_is_an_authoritative_100_match_with_its_log_line():
    svc = _service()
    pool = svc._prepare_candidate_pool([_ebook(2348, edition_audio_ids=["5114"]), _ebook(77, "Unrelated Audio Title")])

    result = svc._scan_single_audiobook(_audio(), pool)

    assert [(m["source_id"], m["score"], m["match_reason"], m.get("authority")) for m in result["matches"]] == [
        (2348, 100.0, "authoritative", "bookorbit_edition")]
    svc.logger.info.assert_any_call(
        "📌 Authoritative match via BookOrbit edition for 'Unrelated Audio Title' -> "
        "Different Ebook Title-2348.epub")


def test_unlinked_audiobook_uses_normal_scoring():
    svc = _service()
    pool = svc._prepare_candidate_pool([_ebook(2348, "Unrelated Audio Title", edition_audio_ids=["999"])])

    result = svc._scan_single_audiobook(_audio(), pool)

    assert all(m.get("match_reason") != "authoritative" and "authority" not in m for m in result["matches"])


def test_two_ebook_editions_fall_back_to_normal_scoring():
    svc = _service()
    pool = svc._prepare_candidate_pool([
        _ebook(2348, edition_audio_ids=["5114"]), _ebook(2349, edition_audio_ids=["5114"])])

    assert svc._scan_single_audiobook(_audio(), pool) is None


def test_one_ebook_with_two_files_is_still_one_edition():
    svc = _service()
    pool = svc._prepare_candidate_pool([
        _ebook(2348, edition_audio_ids=["5114"]), _ebook(2348, "Other File", edition_audio_ids=["5114"])])

    assert svc._scan_single_audiobook(_audio(), pool)["matches"][0]["authority"] == "bookorbit_edition"


def test_known_language_mismatch_falls_back_to_normal_scoring():
    svc = _service()
    pool = svc._prepare_candidate_pool([_ebook(2348, edition_audio_ids=["5114"], language="en")])

    assert svc._scan_single_audiobook(_audio(audio_language="es"), pool) is None
    assert svc._scan_single_audiobook(_audio(audio_language="en-US"), pool)["matches"][0]["score"] == 100.0


def test_edition_ids_only_count_for_bookorbit_audio():
    svc = _service()
    pool = svc._prepare_candidate_pool([_ebook(2348, edition_audio_ids=["5114"])])

    assert svc._scan_single_audiobook(_audio(audio_source="ABS", bridge_key="5114"), pool) is None


def test_calibre_identifier_match_is_unchanged_and_wins():
    svc = _service()
    calibre = _ebook("c1", "Calibre Copy", source="CWA")
    calibre.abs_identifier = "5114"
    pool = svc._prepare_candidate_pool([_ebook(2348, edition_audio_ids=["5114"]), calibre])

    match = svc._scan_single_audiobook(_audio(), pool)["matches"][0]

    assert (match["source_id"], match["match_reason"], "authority" in match) == ("c1", "authoritative", False)
    svc.logger.info.assert_any_call(
        "📌 Authoritative match via audiobookshelf_id for 'Unrelated Audio Title' -> Calibre Copy-c1.epub")


def test_reverse_discovery_classifies_a_grouped_ebook_as_has_audio():
    inventory = build_ebook_inventory([{
        "source": "BookOrbit", "source_id": "2348", "title": "Completely Different Ebook Title",
        "author": "Different Ebook Author", "edition_audio_ids": [5114],
    }])
    pool = [_audio(), {**_audio(), "audio_source": "ABS", "bridge_key": "abs-5114"}]

    entry = next(iter(build_discovery_snapshot(_service(), inventory, pool, now=1)["entries"].values()))

    assert (entry["classification"], entry["audio_keys"], entry["authoritative_match"]) == (
        "has_audio", ["bookorbit:5114"], True)


@pytest.mark.parametrize("enabled, linked", [(None, False), ("false", False), ("true", True)])
def test_edition_pair_auto_links_only_when_auto_match_is_on(enabled, linked):
    svc = _service()
    pool = svc._prepare_candidate_pool([_ebook(2348, edition_audio_ids=["5114"])])
    suggestion = svc._scan_single_audiobook(_audio(), pool)
    env = {} if enabled is None else {"SUGGESTIONS_AUTO_MATCH_ENABLED": enabled}
    with patch.dict(os.environ, env), patch("src.web_server.container") as container:
        if enabled is None:
            os.environ.pop("SUGGESTIONS_AUTO_MATCH_ENABLED", None)
        mapping = container.book_mapping_service.return_value
        mapping.create_audio_mapping_from_match.return_value = True
        out = _auto_match_suggestions({"suggestions": [suggestion], "stats": {}}, user_id=1)

    assert mapping.create_audio_mapping_from_match.called is linked
    assert out["suggestions"] == ([] if linked else [suggestion])


def test_search_path_ebooks_carry_edition_ids_from_the_list_cache(client):
    client._book_cache = {2348: client._build_light_info(LINKED_CARD)}
    detail = {"title": "Shared Book", "files": [{"id": 11, "format": "epub", "filename": "Shared Book.epub"}]}
    with patch.object(client, "get_book_detail", return_value=detail):
        assert client._enrich_ebook(2348, {})["editionAudioIds"] == ["5114"]
        assert client._enrich_ebook(1, {})["editionAudioIds"] == []


def test_discover_labels_bookorbit_editions_apart_from_calibre_identifiers():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "templates/next/_discover_main_script.html").read_text(encoding="utf-8")
    assert "bookorbit_edition: {{ _('BookOrbit edition')|tojson }}" in script
    assert "const reason = top.authority === 'bookorbit_edition' ? top.authority" in script
