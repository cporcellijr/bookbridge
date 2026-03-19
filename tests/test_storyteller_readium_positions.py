import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.storyteller_api import StorytellerAPIClient
from src.sync_clients.storyteller_sync_client import StorytellerSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _build_storyteller_client() -> StorytellerAPIClient:
    os.environ.setdefault("STORYTELLER_API_URL", "http://storyteller.local")
    return StorytellerAPIClient()


def test_get_readium_positions_accepts_array_root():
    client = _build_storyteller_client()
    expected = [{"href": "Text/ch1.xhtml", "locations": {"progression": 0.5, "position": 9}}]
    client._make_request = MagicMock(return_value=SimpleNamespace(status_code=200, json=lambda: expected))

    result = client.get_readium_positions("book-1")

    assert result == expected
    client._make_request.assert_called_once_with(
        "GET",
        "/api/v2/books/book-1/read/~readium/positions.json",
    )


def test_get_readium_positions_accepts_wrapped_positions():
    client = _build_storyteller_client()
    expected = [{"href": "Text/ch2.xhtml", "locations": {"progression": 0.2, "position": 4}}]
    client._make_request = MagicMock(
        return_value=SimpleNamespace(status_code=200, json=lambda: {"positions": expected})
    )

    assert client.get_readium_positions("book-2") == expected


def test_get_readium_positions_returns_empty_list_on_failure():
    client = _build_storyteller_client()
    client._make_request = MagicMock(return_value=SimpleNamespace(status_code=500, json=lambda: {}))
    assert client.get_readium_positions("book-3") == []

    def _raise_json():
        raise ValueError("bad json")

    client._make_request = MagicMock(return_value=SimpleNamespace(status_code=200, json=_raise_json))
    assert client.get_readium_positions("book-3") == []


def test_resolve_exact_position_finds_closest_progression_on_matching_href():
    client = _build_storyteller_client()
    client.get_readium_positions = MagicMock(
        return_value=[
            {"href": "Text%2Fch1.xhtml", "locations": {"progression": 0.10, "position": 1}},
            {"href": "Text%2Fch1.xhtml", "locations": {"progression": 0.60, "position": 8}},
            {"href": "Text%2Fch1.xhtml", "locations": {"progression": 0.52, "position": 7}},
            {"href": "Text%2Fch2.xhtml", "locations": {"progression": 0.52, "position": 99}},
        ]
    )

    assert client.resolve_exact_position("book-1", "Text/ch1.xhtml", 0.50) == 7


def test_resolve_exact_position_ignores_non_matching_href():
    client = _build_storyteller_client()
    client.get_readium_positions = MagicMock(
        return_value=[
            {"href": "Text/ch2.xhtml", "locations": {"progression": 0.50, "position": 10}},
        ]
    )

    assert client.resolve_exact_position("book-1", "Text/ch1.xhtml", 0.50) is None


def test_resolve_exact_position_tie_breaks_to_lower_position():
    client = _build_storyteller_client()
    client.get_readium_positions = MagicMock(
        return_value=[
            {"href": "Text/ch1.xhtml", "locations": {"progression": 0.40, "position": 10}},
            {"href": "Text/ch1.xhtml", "locations": {"progression": 0.60, "position": 3}},
        ]
    )

    assert client.resolve_exact_position("book-1", "Text/ch1.xhtml", 0.50) == 3


def test_update_position_injects_exact_readium_position_when_resolved():
    client = _build_storyteller_client()
    client.resolve_exact_position = MagicMock(return_value=42)
    captured = {}

    def _fake_make_request(method, endpoint, json_data=None):
        if method == "POST":
            captured["payload"] = json_data
            return SimpleNamespace(status_code=204, text="")
        return SimpleNamespace(status_code=404, text="")

    client._make_request = MagicMock(side_effect=_fake_make_request)
    locator = LocatorResult(percentage=0.35, href="Text/ch1.xhtml", chapter_progress=0.7)

    assert client.update_position("book-1", 0.35, locator) is True
    assert captured["payload"]["locator"]["locations"]["position"] == 42
    assert captured["payload"]["locator"]["locations"]["progression"] == 0.7
    assert captured["payload"]["locator"]["locations"]["totalProgression"] == 0.35


def test_update_position_posts_unquoted_href_for_guided_navigation_matching():
    client = _build_storyteller_client()
    client.resolve_exact_position = MagicMock(return_value=42)
    captured = {}

    def _fake_make_request(method, endpoint, json_data=None):
        if method == "POST":
            captured["payload"] = json_data
            return SimpleNamespace(status_code=204, text="")
        return SimpleNamespace(status_code=404, text="")

    client._make_request = MagicMock(side_effect=_fake_make_request)
    locator = LocatorResult(
        percentage=0.35,
        href="e9781668077092%2Fxhtml%2Fch16.xhtml",
        fragment="ch16-sentence4",
        chapter_progress=0.7,
    )

    assert client.update_position("book-1", 0.35, locator) is True
    assert captured["payload"]["locator"]["href"] == "e9781668077092/xhtml/ch16.xhtml"


def test_update_position_omits_exact_position_when_unresolved():
    client = _build_storyteller_client()
    client.resolve_exact_position = MagicMock(return_value=None)
    captured = {}

    def _fake_make_request(method, endpoint, json_data=None):
        if method == "POST":
            captured["payload"] = json_data
            return SimpleNamespace(status_code=204, text="")
        return SimpleNamespace(status_code=404, text="")

    client._make_request = MagicMock(side_effect=_fake_make_request)
    locator = LocatorResult(percentage=0.25, href="Text/ch1.xhtml", chapter_progress=0.4)

    assert client.update_position("book-1", 0.25, locator) is True
    assert "position" not in captured["payload"]["locator"]["locations"]
    assert captured["payload"]["locator"]["locations"]["progression"] == 0.4
    assert captured["payload"]["locator"]["locations"]["totalProgression"] == 0.25


def test_storyteller_sync_client_preserves_chapter_progress_for_update_call():
    storyteller_api = MagicMock()
    storyteller_api.update_position.return_value = True
    ebook_parser = MagicMock()
    ebook_parser.resolve_book_path.return_value = "/tmp/storyteller_st-uuid-9.epub"
    ebook_parser.find_text_location.return_value = LocatorResult(
        percentage=0.61,
        href="OEBPS/Text/part0083.xhtml",
        fragment="x_c079-sentence123",
        chapter_progress=0.73,
    )

    client = StorytellerSyncClient(storyteller_api, ebook_parser)
    book = SimpleNamespace(
        abs_id="abs-9",
        abs_title="Test",
        ebook_filename="original.epub",
        original_ebook_filename="original.epub",
        storyteller_uuid="st-uuid-9",
    )
    request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.61), txt="anchor text")

    result = client.update_progress(book, request)

    assert result.success is True
    args = storyteller_api.update_position.call_args[0]
    assert args[0] == "st-uuid-9"
    assert args[2].chapter_progress == 0.73


def test_get_position_details_payload_unquotes_href():
    client = _build_storyteller_client()
    response_payload = {
        "timestamp": 1773951460302,
        "locator": {
            "href": "e9781668077092%2Fxhtml%2Fch16.xhtml",
            "locations": {
                "totalProgression": 0.5907,
                "progression": 0.0678,
                "position": 129,
                "fragments": ["ch16-sentence4"],
            },
        },
    }
    client._make_request = MagicMock(
        return_value=SimpleNamespace(status_code=200, json=lambda: response_payload)
    )

    payload = client.get_position_details_payload("book-1")

    assert payload["href"] == "e9781668077092/xhtml/ch16.xhtml"
