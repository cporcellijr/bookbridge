from types import SimpleNamespace
from unittest.mock import MagicMock

from src.sync_clients.booklore_sync_client import BookloreSyncClient


def test_booklore_service_state_derives_pct_from_cfi_when_percentage_missing():
    booklore_client = MagicMock()
    booklore_client.is_configured.return_value = True
    booklore_client.get_progress_details.return_value = {
        "pct": None,
        "raw_pct": None,
        "cfi": "epubcfi(/6/10!/4:0)",
        "href": None,
        "positioned": True,
    }

    ebook_parser = MagicMock()
    ebook_parser.resolve_cfi_to_index.return_value = 250
    ebook_parser.resolve_book_path.return_value = "book.epub"
    ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])

    client = BookloreSyncClient(booklore_client, ebook_parser)
    book = SimpleNamespace(original_ebook_filename="book.epub", ebook_filename=None)

    state = client.get_service_state(book, prev_state=None)

    assert state is not None
    assert state.current["pct"] == 0.25
    assert state.current["cfi"] == "epubcfi(/6/10!/4:0)"


def test_booklore_service_state_returns_none_when_no_position_and_no_percentage():
    booklore_client = MagicMock()
    booklore_client.is_configured.return_value = True
    booklore_client.get_progress_details.return_value = {
        "pct": None,
        "raw_pct": None,
        "cfi": None,
        "href": None,
        "positioned": False,
    }

    client = BookloreSyncClient(booklore_client, MagicMock())
    book = SimpleNamespace(original_ebook_filename="book.epub", ebook_filename=None)

    assert client.get_service_state(book, prev_state=None) is None


def test_booklore_supports_book_uses_cache_only_for_non_booklore_sources():
    booklore_client = MagicMock()
    booklore_client.find_book_by_filename.return_value = None

    client = BookloreSyncClient(booklore_client, MagicMock())
    book = SimpleNamespace(
        original_ebook_filename="book.epub",
        ebook_filename=None,
        ebook_source="Kavita",
    )

    assert client.supports_book(book) is False
    booklore_client.find_book_by_filename.assert_called_once_with("book.epub", allow_refresh=False)
