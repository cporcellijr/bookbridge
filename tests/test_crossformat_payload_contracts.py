import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.api_clients import ABSClient, KoSyncClient
from src.api.booklore_client import BookloreClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def test_kosync_payload_contract_keys_and_degraded_progress_only():
    with patch.dict(
        os.environ,
        {"KOSYNC_SERVER": "http://kosync.local", "KOSYNC_USER": "user", "KOSYNC_KEY": "secret"},
        clear=False,
    ):
        client = KoSyncClient()
        payloads = []

        def _fake_put(url, headers=None, json=None, timeout=None):
            payloads.append(dict(json))
            return SimpleNamespace(status_code=200, text="ok")

        client.session.put = MagicMock(side_effect=_fake_put)

        with patch("src.api.api_clients.time.time", return_value=1700000000):
            assert client.update_progress("doc-1", 0.42, "/body/DocFragment[1]/body/p[1]/text().0")
            assert client.update_progress("doc-1", 0.42, None)

        expected_keys = {"document", "percentage", "progress", "device", "device_id"}
        assert set(payloads[0].keys()) == expected_keys
        assert set(payloads[1].keys()) == expected_keys

        assert payloads[0]["progress"] == "/body/DocFragment[1]/body/p[1]/text().0"
        assert payloads[1]["progress"] == ""
        p0_no_progress = {k: v for k, v in payloads[0].items() if k != "progress"}
        p1_no_progress = {k: v for k, v in payloads[1].items() if k != "progress"}
        assert p0_no_progress == p1_no_progress


def test_kosync_payload_contract_keeps_internal_force_fields_for_local_server():
    with patch.dict(
        os.environ,
        {"KOSYNC_SERVER": "http://127.0.0.1:5758", "KOSYNC_USER": "user", "KOSYNC_KEY": "secret"},
        clear=False,
    ):
        client = KoSyncClient()
        payloads = []

        def _fake_put(url, headers=None, json=None, timeout=None):
            payloads.append(dict(json))
            return SimpleNamespace(status_code=202, text="accepted")

        client.session.put = MagicMock(side_effect=_fake_put)

        with patch("src.api.api_clients.time.time", return_value=1700000000):
            assert client.update_progress("doc-1", 0.42, "/body/DocFragment[1]/body/p[1]/text().0")

        expected_keys = {"document", "percentage", "progress", "device", "device_id", "timestamp", "force"}
        assert set(payloads[0].keys()) == expected_keys
        assert payloads[0]["timestamp"] == 1700000000
        assert payloads[0]["force"] is True


def test_booklore_payload_contract_epub_percentage_and_optional_cfi():
    client = BookloreClient(database_service=None)
    client.find_book_by_filename = MagicMock(
        return_value={"id": "book-1", "bookType": "EPUB", "fileName": "book.epub"}
    )
    client._book_id_cache = {"book-1": {"id": "book-1", "epubProgress": {}}}

    payloads = []

    def _fake_make_request(method, endpoint, payload):
        payloads.append(payload)
        return SimpleNamespace(status_code=200)

    client._make_request = MagicMock(side_effect=_fake_make_request)
    client._get_progress_by_book_id = MagicMock(side_effect=[(0.42, None), (0.36, None)])

    with_cfi = LocatorResult(percentage=0.42, cfi="epubcfi(/6/10!/4:0)")
    without_cfi = LocatorResult(percentage=0.36)

    assert client.update_progress("book.epub", 0.42, with_cfi)
    assert client.update_progress("book.epub", 0.36, without_cfi)

    first = payloads[0]
    second = payloads[1]

    assert first["bookId"] == "book-1"
    assert "epubProgress" in first
    assert set(first["epubProgress"].keys()) == {"percentage", "cfi"}
    assert first["epubProgress"]["percentage"] == 42.0
    assert first["epubProgress"]["cfi"] == "epubcfi(/6/10!/4:0)"

    assert second["bookId"] == "book-1"
    assert "epubProgress" in second
    assert "percentage" in second["epubProgress"]
    assert second["epubProgress"]["percentage"] == 36.0
    assert "cfi" not in second["epubProgress"]


def test_abs_sync_payload_contract_current_time_and_time_listened():
    with patch.dict(os.environ, {"ABS_SERVER": "http://abs.local", "ABS_KEY": "token"}, clear=False):
        client = ABSClient()
        client.create_session = MagicMock(return_value="session-1")
        client.close_session = MagicMock(return_value=True)

        captured = {}

        def _fake_post(url, json=None, timeout=None):
            captured["payload"] = dict(json)
            return SimpleNamespace(status_code=200, text="ok")

        client.session.post = MagicMock(side_effect=_fake_post)

        result = client.update_progress("abs-id-1", 123.4, 8.6)
        assert result["success"] is True
        assert set(captured["payload"].keys()) == {"currentTime", "timeListened"}
        assert captured["payload"]["currentTime"] == 123.4
        assert captured["payload"]["timeListened"] == 8.6


def test_storyteller_update_position_call_shape_preserved():
    storyteller_api = MagicMock()
    storyteller_api.update_position.return_value = True
    ebook_parser = MagicMock()
    client = StorytellerSyncClient(storyteller_api, ebook_parser)

    book = SimpleNamespace(
        abs_id="abs-1",
        abs_title="Test",
        ebook_filename="book.epub",
        storyteller_uuid="st-uuid-1",
    )
    locator = LocatorResult(percentage=0.55, href="chapter.xhtml", fragment="frag-1")
    request = UpdateProgressRequest(locator_result=locator, txt="irrelevant")

    result = client.update_progress(book, request)

    assert result.success is True
    storyteller_api.update_position.assert_called_once()
    args = storyteller_api.update_position.call_args[0]
    assert len(args) == 3
    assert args[0] == "st-uuid-1"
    assert args[1] == 0.55
    assert isinstance(args[2], LocatorResult)
    assert args[2].href == "chapter.xhtml"


def test_storyteller_update_prefers_storyteller_epub_remap_when_text_available():
    storyteller_api = MagicMock()
    storyteller_api.update_position.return_value = True
    ebook_parser = MagicMock()
    ebook_parser.resolve_book_path.return_value = "/tmp/storyteller_st-uuid-9.epub"
    ebook_parser.find_text_location.return_value = LocatorResult(
        percentage=0.61,
        href="OEBPS/Text/part0083.xhtml",
        fragment="x_c079-sentence123",
    )
    client = StorytellerSyncClient(storyteller_api, ebook_parser)

    book = SimpleNamespace(
        abs_id="abs-9",
        abs_title="Test",
        ebook_filename="original.epub",
        original_ebook_filename="original.epub",
        storyteller_uuid="st-uuid-9",
    )
    locator = LocatorResult(percentage=0.61, href="orig.xhtml", fragment="orig-frag")
    request = UpdateProgressRequest(locator_result=locator, txt="anchor text from leader")

    result = client.update_progress(book, request)

    assert result.success is True
    ebook_parser.find_text_location.assert_called_once_with(
        "storyteller_st-uuid-9.epub",
        "anchor text from leader",
        hint_percentage=0.61,
    )
    args = storyteller_api.update_position.call_args[0]
    assert args[0] == "st-uuid-9"
    assert args[2].href == "OEBPS/Text/part0083.xhtml"
    assert args[2].fragment == "x_c079-sentence123"


def test_storyteller_get_service_state_includes_rich_locator_fields():
    storyteller_api = MagicMock()
    storyteller_api.is_configured.return_value = True
    storyteller_api.get_position_details_payload.return_value = {
        "pct": 0.4,
        "ts": 1234,
        "href": "Text/chapter-1.xhtml",
        "frag": "frag-1",
        "fragment": "frag-1",
        "fragments": ["frag-1", "frag-2"],
        "chapter_progress": 0.2,
        "css_selector": "p:nth-child(2)",
        "position": 17,
        "match_index": 17,
        "cfi": "epubcfi(/6/4!/4:0)",
    }
    ebook_parser = MagicMock()
    client = StorytellerSyncClient(storyteller_api, ebook_parser)

    book = SimpleNamespace(storyteller_uuid="uuid-1")
    state = client.get_service_state(book, None)

    assert state.current["href"] == "Text/chapter-1.xhtml"
    assert state.current["frag"] == "frag-1"
    assert state.current["fragment"] == "frag-1"
    assert state.current["fragments"] == ["frag-1", "frag-2"]
    assert state.current["chapter_progress"] == 0.2
    assert state.current["css_selector"] == "p:nth-child(2)"
    assert state.current["position"] == 17
    assert state.current["match_index"] == 17
    assert state.current["cfi"] == "epubcfi(/6/4!/4:0)"


def test_storyteller_update_returns_rich_updated_state():
    storyteller_api = MagicMock()
    storyteller_api.update_position.return_value = True
    ebook_parser = MagicMock()
    ebook_parser.resolve_book_path.return_value = "/tmp/storyteller_uuid.epub"
    ebook_parser.find_text_location.return_value = LocatorResult(
        percentage=0.61,
        href="OEBPS/Text/part0083.xhtml",
        fragment="x_c079-sentence123",
        cfi="epubcfi(/6/8!/4:0)",
        css_selector="p:nth-child(3)",
        chapter_progress=0.45,
        match_index=312,
        fragments=["x_c079-sentence123"],
    )
    client = StorytellerSyncClient(storyteller_api, ebook_parser)

    book = SimpleNamespace(
        abs_id="abs-9",
        abs_title="Test",
        ebook_filename="original.epub",
        storyteller_uuid="st-uuid-9",
    )
    request = UpdateProgressRequest(
        locator_result=LocatorResult(percentage=0.61, href="orig.xhtml"),
        txt="anchor text from leader",
    )

    result = client.update_progress(book, request)

    assert result.success is True
    assert result.updated_state == {
        "pct": 0.61,
        "href": "OEBPS/Text/part0083.xhtml",
        "frag": "x_c079-sentence123",
        "fragment": "x_c079-sentence123",
        "fragments": ["x_c079-sentence123"],
        "cfi": "epubcfi(/6/8!/4:0)",
        "chapter_progress": 0.45,
        "css_selector": "p:nth-child(3)",
        "position": 312,
        "match_index": 312,
    }

