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
    assert BookOrbitClient._edition_ids(card) == ["8"]
    assert BookOrbitClient._edition_ids(card, "ebook") == ["8"]
    assert BookOrbitClient._edition_ids({"linked": None}) == []


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


# --- Edition links when a book is read or listened to (shelf/reading/listening watch, KOReader) ---

from src.services.shelf_watch_service import ShelfWatchService  # noqa: E402

BOOKORBIT_AUDIO = {"audio_source": "BookOrbit", "audio_source_id": "5114", "bridge_key": "bookorbit:5114",
                   "audio_title": "Unrelated Audio Title", "audio_author": "Narrated Author"}


def _edition_scanner(pool=(BOOKORBIT_AUDIO,)):
    svc = _service()
    svc.database_service.get_book.return_value = None
    svc._build_audiobook_candidate_pool = lambda *_a, **_k: [dict(c) for c in pool]
    return svc


def _ebook_anchor(**extra):
    return {"filename": "a.epub", "title": "Different Ebook Title", "authors": "Someone Else",
            "edition_audio_ids": ["5114"], **extra}


def test_ebook_scan_returns_the_grouped_audiobook_as_one_authoritative_match():
    svc = _edition_scanner()
    result = svc._scan_single_ebook(_ebook_anchor(), svc._build_audiobook_candidate_pool())

    assert [(m["bridge_key"], m["score"], m["match_reason"], m["authority"]) for m in result["matches"]] == [
        ("bookorbit:5114", 100.0, "authoritative", "bookorbit_edition")]
    svc.logger.info.assert_any_call(
        "📌 Authoritative match via BookOrbit edition for 'Different Ebook Title' -> Unrelated Audio Title")


@pytest.mark.parametrize("anchor, pool, mapped", [
    (_ebook_anchor(edition_audio_ids=["5114", "5115"]),
     (BOOKORBIT_AUDIO, {**BOOKORBIT_AUDIO, "audio_source_id": "5115", "bridge_key": "bookorbit:5115"}), None),
    (_ebook_anchor(language="en"), ({**BOOKORBIT_AUDIO, "audio_language": "es"},), None),
    (_ebook_anchor(), ({**BOOKORBIT_AUDIO, "audio_source": "ABS", "bridge_key": "5114"},), None),
    (_ebook_anchor(), (BOOKORBIT_AUDIO,), MagicMock(abs_id="bookorbit:5114")),
])
def test_ebook_scan_falls_back_for_two_audiobooks_language_other_source_or_mapped(anchor, pool, mapped):
    svc = _edition_scanner(pool)
    svc.database_service.get_book.return_value = mapped
    result = svc._scan_single_ebook(anchor, svc._build_audiobook_candidate_pool())

    assert all("authority" not in m for m in (result or {}).get("matches", []))


def _watch(client, scanner=None, mapped_audio=None):
    db = MagicMock()
    db.get_book.side_effect = lambda key: mapped_audio if key == "bookorbit:5114" else None
    db.get_book_by_ebook_filename.return_value = None
    db.get_book_by_ebook_source.return_value = None
    db.get_shelf_watch_scan.return_value = None
    mapping = MagicMock()
    mapping.create_audio_mapping_from_match.return_value = MagicMock(
        abs_id="bookorbit:5114", audio_source="BookOrbit", audio_source_id="5114")
    client.is_configured.return_value = True
    watch = ShelfWatchService(booklore_client=client, database_service=db, book_mapping_service=mapping,
                              suggestions_service_factory=lambda: scanner or _edition_scanner(),
                              source_name="BookOrbit", env_prefix="BOOKORBIT")
    return watch, mapping


WATCH_ON = {"BOOKORBIT_READING_WATCH_ENABLED": "true", "BOOKORBIT_SHELF_WATCH_ENABLED": "false",
            "BOOKORBIT_SHELF_WATCH_READALONG": "false"}


def test_reading_an_ebook_maps_its_grouped_audiobook():
    client = MagicMock()
    client.list_continue_reading_books.return_value = [{
        "id": 2348, "title": "Different Ebook Title", "author": "Someone Else", "fileName": "a.epub",
        "progress": 5.0, "editionAudioIds": ["5114"]}]
    client.list_continue_listening_books.return_value = []
    watch, mapping = _watch(client)
    with patch.dict(os.environ, WATCH_ON):
        stats = watch._process_reading_watch(user_id=None)

    assert stats["auto_matched"] == 1
    kwargs = mapping.create_audio_mapping_from_match.call_args.kwargs
    assert (kwargs["audio_source"], kwargs["audio_source_id"], kwargs["ebook_source_id"]) == ("BookOrbit", "5114", "2348")


def _listening_client(ebook_ids=("2348",), audio_language="", ebook_language=""):
    client = MagicMock()
    client.list_continue_reading_books.return_value = []
    client.list_continue_listening_books.return_value = [
        {"id": 5114, "title": "Audio Title", "language": audio_language, "editionEbookIds": list(ebook_ids)}]
    client._enrich_ebook.return_value = {"id": "2348", "title": "Ebook Title", "fileName": "a.epub",
                                         "language": ebook_language}
    return client


def test_listening_to_an_audiobook_maps_its_one_grouped_ebook():
    client = _listening_client()
    watch, mapping = _watch(client)
    with patch.dict(os.environ, WATCH_ON):
        watch.process_watch_shelf()

    kwargs = mapping.create_audio_mapping_from_match.call_args.kwargs
    assert (kwargs["audio_source"], kwargs["audio_source_id"], kwargs["ebook_filename"],
            kwargs["ebook_source"], kwargs["ebook_source_id"]) == ("BookOrbit", "5114", "a.epub", "BookOrbit", "2348")
    client._enrich_ebook.assert_called_once_with("2348", {})


@pytest.mark.parametrize("ebook_ids, languages, mapped_audio, enabled", [
    (("2348", "2349"), ("", ""), None, "true"),
    ((), ("", ""), None, "true"),
    (("2348",), ("es", "en"), None, "true"),
    (("2348",), ("", ""), MagicMock(abs_id="bookorbit:5114"), "true"),
    (("2348",), ("", ""), None, "false"),
])
def test_listening_watch_leaves_ambiguous_mapped_or_disabled_audiobooks_alone(ebook_ids, languages, mapped_audio, enabled):
    client = _listening_client(ebook_ids, *languages)
    watch, mapping = _watch(client, mapped_audio=mapped_audio)
    with patch.dict(os.environ, {**WATCH_ON, "BOOKORBIT_READING_WATCH_ENABLED": enabled}):
        assert watch._process_listening_watch() == 0

    mapping.create_audio_mapping_from_match.assert_not_called()


def test_client_lists_continue_listening_with_ebook_editions(client):
    response = MagicMock(status_code=200)
    response.json.return_value = {"books": [
        {"id": 5114, "title": " Audio ", "linked": {"books": [{"bookId": 2348, "formats": ["epub"]}]}},
        {"id": 7, "title": "Cached"}]}
    client._book_cache = {7: {"id": 7, "title": "Cached", "language": "en", "editionEbookIds": ["8"]}}
    with patch.object(client, "_make_request", return_value=response) as request:
        books = client.list_continue_listening_books()

    request.assert_called_once_with("GET", "/api/v1/dashboard/scrollers/continue-listening?limit=100")
    assert books == [{"id": 5114, "title": "Audio", "language": "", "editionEbookIds": ["2348"]},
                     {"id": 7, "title": "Cached", "language": "en", "editionEbookIds": ["8"]}]


def test_continue_reading_and_shelf_books_carry_cached_audio_editions(client):
    client._book_cache = {2348: client._build_light_info(LINKED_CARD)}
    response = MagicMock(status_code=200)
    response.json.return_value = {"books": [{"id": 2348, "title": "Shared Book", "readingProgress": 5}]}
    with patch.object(client, "_make_request", return_value=response), \
            patch.object(client, "get_book_detail", return_value=None):
        assert client.list_continue_reading_books()[0]["editionAudioIds"] == ["5114"]
    response.json.return_value = {"items": [{"id": 2348, "title": "Shared Book"}]}
    with patch.object(client, "_make_request", return_value=response), \
            patch.object(client, "_get_collection_id", return_value=1), \
            patch.object(client, "get_book_detail", return_value=None):
        assert client.list_books_on_shelf("Up Next")[0]["editionAudioIds"] == ["5114"]


def test_koreader_discovery_maps_an_unknown_ebook_to_its_grouped_audiobook():
    from src.api import kosync_server as ks
    bookorbit = MagicMock()
    bookorbit.get_book_by_id.side_effect = lambda book_id, allow_refresh=True: {
        2348: {"id": 2348, "title": "Ebook", "language": "en", "editionAudioIds": ["5114"]},
        5114: {"id": 5114, "title": "Audio Title", "language": ""},
    }.get(book_id)
    db = MagicMock()
    db.get_book_by_ebook_filename.return_value = None
    db.get_book.return_value = None
    container = MagicMock()
    container.ebook_parser.return_value.get_book_metadata.return_value = {}
    mapping = container.book_mapping_service.return_value
    mapping.create_audio_mapping_from_match.return_value = MagicMock(
        abs_id="bookorbit:5114", ebook_source="BookOrbit", ebook_source_id="2348")
    with patch.dict(os.environ, {"KOSYNC_AUTO_MAP_ON_AGREEMENT": "true"}), \
            patch.object(ks, "_database_service", db), patch.object(ks, "_container", container), \
            patch.object(ks, "_manager", None), \
            patch.object(ks, "_resolve_book_by_cwa_checksum", return_value=None), \
            patch.object(ks, "_resolve_book_by_epub_identifier", return_value=None), \
            patch.object(ks, "_resolve_library_ebook_source", return_value=("BookOrbit", "2348")), \
            patch.object(ks, "_resolve_abs_ebook_item", return_value=None), \
            patch.object(ks, "_discovery_client", return_value=bookorbit), \
            patch.object(ks, "_autodiscovery_audiobook_candidates", side_effect=AssertionError("fuzzy path")):
        ks._run_put_auto_discovery_inner("ab" * 16, None, epub_filename="a.epub")

    kwargs = mapping.create_audio_mapping_from_match.call_args.kwargs
    assert (kwargs["audio_source"], kwargs["audio_source_id"], kwargs["audio_cover_url"],
            kwargs["ebook_source_id"], kwargs["kosync_doc_id"]) == ("BookOrbit", "5114", None, "2348", "ab" * 16)
    db.link_kosync_document.assert_called_once_with("ab" * 16, "bookorbit:5114")


@pytest.mark.parametrize("source, audio_ids, audio_language, mapped", [
    ("BookOrbit", ["5114", "5115"], "", None),
    ("BookOrbit", ["5114"], "es", None),
    ("BookOrbit", ["5114"], "", MagicMock(abs_id="bookorbit:5114")),
    ("BookLore", ["5114"], "", None),
])
def test_koreader_edition_candidate_needs_one_unmapped_same_language_bookorbit_audiobook(
        source, audio_ids, audio_language, mapped):
    from src.api import kosync_server as ks
    bookorbit = MagicMock()
    bookorbit.get_book_by_id.side_effect = lambda book_id, allow_refresh=True: {
        2348: {"id": 2348, "title": "Ebook", "language": "en", "editionAudioIds": audio_ids},
        5114: {"id": 5114, "title": "Audio Title", "language": audio_language},
    }.get(book_id)
    db = MagicMock()
    db.get_book.side_effect = lambda key: mapped if key == "bookorbit:5114" else None
    with patch.object(ks, "_database_service", db), \
            patch.object(ks, "_resolve_library_ebook_source", return_value=(source, "2348")), \
            patch.object(ks, "_discovery_client", return_value=bookorbit):
        assert ks._bookorbit_edition_audio_candidate("a.epub") is None


def test_manual_search_lists_the_grouped_ebook_first():
    from src.web.suggestions_ebook_catalog import _promote_authoritative_ebook_matches
    audiobooks = [SimpleNamespace(source="BookOrbit", source_id=5114)]
    ebooks = [SimpleNamespace(name="other.epub", source="BookOrbit", abs_identifier=None, edition_audio_ids=["999"]),
              SimpleNamespace(name="grouped.epub", source="BookOrbit", abs_identifier=None, edition_audio_ids=["5114"])]

    assert [e.name for e in _promote_authoritative_ebook_matches(audiobooks, ebooks)] == ["grouped.epub", "other.epub"]
