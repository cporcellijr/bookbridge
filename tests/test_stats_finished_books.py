"""The stats recaps count the books ABS marks finished, not every book listened to."""

from datetime import datetime
from zoneinfo import ZoneInfo

from unittest.mock import MagicMock, patch

import src.web_server as web_server
from src.web_server import _abs_finished_books, _build_combined_yearly_recap, _build_listening_yearly_recap

DENVER = ZoneInfo("America/Denver")


def _ms(year, month, day):
    return int(datetime(year, month, day, 12, tzinfo=DENVER).timestamp() * 1000)


def test_finished_books_come_from_abs_progress_with_titles():
    progress = {
        "book-a": {"libraryItemId": "book-a", "isFinished": True, "finishedAt": _ms(2026, 8, 20)},
        "book-b": {"libraryItemId": "book-b", "isFinished": False, "finishedAt": None},
        "pod-c": {"libraryItemId": "pod-c", "isFinished": True, "finishedAt": _ms(2026, 9, 1), "episodeId": "ep-1"},
        "book-d": {"libraryItemId": "book-d", "isFinished": True, "finishedAt": None},
    }
    items = {"book-a": {"mediaMetadata": {"title": "Dark Matter", "authors": [{"id": "1", "name": "Blake Crouch"}]}}}

    books = _abs_finished_books(progress, items, DENVER)

    assert books == [{
        "bookKey": "abs:book-a", "absId": "book-a", "title": "Dark Matter", "author": "Blake Crouch",
        "finishedAt": _ms(2026, 8, 20) // 1000, "finishedDate": "2026-08-20", "month": 8,
    }]


def test_finish_date_is_the_calendar_day_in_the_server_time_zone():
    # 9:55 PM in Denver is already the next day in UTC.
    evening = int(datetime(2026, 9, 16, 21, 55, tzinfo=DENVER).timestamp() * 1000)
    progress = {"book-a": {"libraryItemId": "book-a", "isFinished": True, "finishedAt": evening}}

    items = {"book-a": {"mediaMetadata": {"title": "White Noise"}}}

    (book,) = _abs_finished_books(progress, items, DENVER)

    assert book["finishedDate"] == "2026-09-16"


def test_listening_recap_counts_books_finished_that_year():
    finished = [
        {"bookKey": "abs:a", "absId": "a", "title": "A", "author": None, "finishedAt": _ms(2026, 8, 20) // 1000, "month": 8},
        {"bookKey": "abs:b", "absId": "b", "title": "B", "author": None, "finishedAt": _ms(2025, 3, 2) // 1000, "month": 3},
    ]

    recap = _build_listening_yearly_recap({"2026-08-20": 600}, 2026, finished, DENVER)

    assert recap["booksFinished"] == 1
    assert [book["absId"] for book in recap["finishedBooks"]] == ["a"]
    assert recap["months"][7]["finished"] == 1


def test_combined_recap_counts_a_book_finished_in_both_formats_once():
    reading = {
        "year": 2026, "months": [{"month": m, "seconds": 0, "pages": 0, "finished": 1 if m == 9 else 0} for m in range(1, 13)],
        "totalPages": 0, "availableYears": [2026],
        "finishedBooks": [{"bookKey": "abs:a", "absId": "a", "title": "A", "author": None,
                           "finishedAt": _ms(2026, 9, 5) // 1000, "month": 9}],
    }
    listening = _build_listening_yearly_recap({}, 2026, [
        {"bookKey": "abs:a", "absId": "a", "title": "A", "author": None, "finishedAt": _ms(2026, 8, 20) // 1000, "month": 8},
        {"bookKey": "abs:b", "absId": "b", "title": "B", "author": None, "finishedAt": _ms(2026, 10, 1) // 1000, "month": 10},
    ], DENVER)

    combined = _build_combined_yearly_recap(reading, listening)

    assert combined["booksFinished"] == 2
    assert sorted(book["absId"] for book in combined["finishedBooks"]) == ["a", "b"]
    assert sum(month["finished"] for month in combined["months"]) == 2


def test_a_year_with_a_finish_but_no_listening_time_is_listed():
    finished = [
        {"bookKey": "abs:b", "absId": "b", "title": "B", "author": None,
         "finishedAt": _ms(2024, 5, 2) // 1000, "finishedDate": "2024-05-02", "month": 5},
    ]

    recap = _build_listening_yearly_recap({"2026-08-20": 600}, 2026, finished, DENVER)

    assert recap["availableYears"] == [2024, 2026]


def test_title_falls_back_to_abs_item_details():
    from unittest.mock import MagicMock
    import src.web_server as web_server
    progress = {
        "no-item": {"libraryItemId": "no-item", "isFinished": True, "finishedAt": _ms(2026, 8, 20)},
    }
    items = {}
    original_db = web_server.database_service
    stub_db = MagicMock()
    stub_db.get_all_books.return_value = []
    web_server.database_service = stub_db
    try:
        class StubClient:
            def get_item_details(self, item_id):
                assert item_id == "no-item"
                return {"media": {"audioFiles": [{"ino": "1"}], "duration": 42852.35,
                                  "metadata": {"title": "First-Time Caller", "authors": [{"name": "A. Writer"}]}}}
        books = _abs_finished_books(progress, items, DENVER, StubClient())
        assert len(books) == 1
        book = books[0]
        assert book["title"] == "First-Time Caller"
        assert book["author"] == "A. Writer"
        # Old 3-arg call still returns Unknown book
        books2 = _abs_finished_books(progress, items, DENVER)
        assert books2[0]["title"] == "Unknown book"
    finally:
        web_server.database_service = original_db


class _ItemDetails:
    """A stand-in ABS client whose item details say whether each item has audio."""

    def __init__(self, media_by_item):
        self.media_by_item = media_by_item

    def get_item_details(self, item_id):
        media = self.media_by_item.get(item_id)
        return None if media is None else {"media": media}


def test_an_ebook_finished_in_abs_is_not_a_listening_finish():
    """ABS keeps ebook reading in the same progress records; an ebook-only item has no audio."""
    progress = {
        "ebook": {"libraryItemId": "ebook", "isFinished": True, "finishedAt": _ms(2026, 8, 20),
                  "duration": 0, "ebookProgress": 1},
        "unplayed": {"libraryItemId": "unplayed", "isFinished": True, "finishedAt": _ms(2026, 8, 21),
                     "duration": 0, "currentTime": 0},
        "unknown": {"libraryItemId": "unknown", "isFinished": True, "finishedAt": _ms(2026, 8, 22),
                    "duration": 0},
    }
    client = _ItemDetails({
        "ebook": {"audioFiles": [], "duration": 0, "metadata": {"title": "An Ebook"}},
        # Marked finished without being played: no duration in its progress, but it has audio.
        "unplayed": {"audioFiles": [{"ino": "1"}], "duration": 42852.35, "metadata": {"title": "First-Time Caller"}},
    })

    with patch.object(web_server, "database_service", MagicMock(get_all_books=MagicMock(return_value=[]))):
        books = _abs_finished_books(progress, {}, DENVER, client)

    # An item ABS can't describe right now is kept rather than silently dropped.
    assert [book["absId"] for book in books] == ["unknown", "unplayed"]
    assert books[1]["title"] == "First-Time Caller"
