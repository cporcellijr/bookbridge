"""The listening book list takes each book's listening time from ABS, like the totals above it."""

from unittest.mock import MagicMock

from src.web_server import _abs_last_listened, _abs_listening_book_list


def test_rows_use_abs_listening_time_and_progress_date():
    items = {"book-a": {"timeListening": 15733.4, "mediaMetadata": {
        "title": "My Struggle", "authors": [{"id": "1", "name": "Karl Ove Knausgaard"}]}}}
    last_listened = {"book-a": 1792975595}

    assert _abs_listening_book_list(items, last_listened, {"book-a": "My Struggle"}) == [{
        "bookKey": "abs:book-a", "absId": "book-a", "isLinked": True,
        "title": "My Struggle", "author": "Karl Ove Knausgaard",
        "totalSeconds": 15733, "sessionCount": None, "avgSessionSeconds": None,
        "lastReadAt": 1792975595,
    }]


def test_items_without_listening_time_are_skipped():
    items = {"a": {"timeListening": 0, "mediaMetadata": {"title": "A"}}, "b": {"mediaMetadata": {"title": "B"}}}

    assert _abs_listening_book_list(items, {}, {}) == []


def test_item_outside_bookbridge_is_unlinked():
    (row,) = _abs_listening_book_list({"x": {"timeListening": 60, "mediaMetadata": {"title": "X"}}}, {}, {})

    assert row["isLinked"] is False
    assert row["title"] == "X"


def test_title_falls_back_to_bookbridge_then_unknown():
    items = {"a": {"timeListening": 60, "mediaMetadata": {}}, "b": {"timeListening": 60}}

    rows = {row["absId"]: row for row in _abs_listening_book_list(items, {}, {"a": "Dark Matter"})}

    assert rows["a"]["title"] == "Dark Matter"
    assert rows["b"]["title"] == "Unknown book"


def test_newest_first_and_undated_last():
    items = {key: {"timeListening": 60, "mediaMetadata": {"title": key}} for key in ("old", "new", "none")}
    last_listened = {"old": 1_000, "new": 2_000}

    assert [row["absId"] for row in _abs_listening_book_list(items, last_listened, {})] == ["new", "old", "none"]


def test_book_without_a_listening_date_is_still_listed():
    (row,) = _abs_listening_book_list({"a": {"timeListening": 60, "mediaMetadata": {"title": "A"}}}, {}, {})

    assert row["lastReadAt"] is None


def test_last_listened_takes_each_books_newest_session():
    client = MagicMock()
    client.get_listening_sessions.side_effect = [[
        {"libraryItemId": "a", "updatedAt": 3_000_000}, {"libraryItemId": "b", "updatedAt": 2_000_000},
        {"libraryItemId": "a", "updatedAt": 1_000_000},
    ]]

    assert _abs_last_listened(client, {"a", "b"}, per_page=3) == {"a": 3000, "b": 2000}
    client.get_listening_sessions.assert_called_once_with(limit=3, page=0)


def test_last_listened_pages_until_every_book_is_dated():
    client = MagicMock()
    client.get_listening_sessions.side_effect = [
        [{"libraryItemId": "a", "updatedAt": 3_000_000}],
        [{"libraryItemId": "b", "updatedAt": 2_000_000}],
    ]

    assert _abs_last_listened(client, {"a", "b"}, per_page=1) == {"a": 3000, "b": 2000}
    assert client.get_listening_sessions.call_count == 2


def test_last_listened_stops_at_a_short_page_and_the_page_cap():
    short = MagicMock()
    short.get_listening_sessions.side_effect = [[{"libraryItemId": "a", "updatedAt": 1_000}]]
    capped = MagicMock()
    capped.get_listening_sessions.return_value = [{"libraryItemId": "x", "updatedAt": 1_000}]

    assert _abs_last_listened(short, {"a", "b"}, per_page=5) == {"a": 1}
    assert _abs_last_listened(capped, {"a"}, per_page=1, max_pages=3) == {}
    assert capped.get_listening_sessions.call_count == 3
