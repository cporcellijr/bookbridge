"""Tests for the new BookloreClient shelf helpers used by the Up Next watcher."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.booklore_client import BookloreClient


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture
def client():
    db = MagicMock()
    db.get_all_booklore_books.return_value = []
    with patch.dict(os.environ, {
        "BOOKLORE_SERVER": "http://mock",
        "BOOKLORE_USER": "u",
        "BOOKLORE_PASSWORD": "p",
        "DATA_DIR": "/tmp/data",
    }):
        return BookloreClient(database_service=db)


# --------------------------------------------------------------------------
# list_books_on_shelf
# --------------------------------------------------------------------------

def test_list_books_on_shelf_returns_books(client):
    with patch.object(client, '_make_request') as mock_req:
        mock_req.side_effect = [
            _Resp([{"id": "shelf-1", "name": "Up Next"}]),  # GET /shelves
            _Resp([{"id": "b1", "title": "Book One"}, {"id": "b2", "title": "Book Two"}]),
        ]
        with patch.object(client, '_parse_json_response', side_effect=lambda r, _label: r.json()):
            books = client.list_books_on_shelf('Up Next')
    assert len(books) == 2
    assert books[0]['id'] == 'b1'


def test_list_books_on_shelf_enriches_minimal_records_from_cache(client):
    """Grimmory's shelf-books endpoint can return minimal {id} dicts; we
    enrich them with fileName/title/authors from the local _book_id_cache."""
    client._book_id_cache = {
        'b1': {'id': 'b1', 'fileName': 'one.epub', 'title': 'Book One', 'authors': 'A'},
        'b2': {'id': 'b2', 'fileName': 'two.epub', 'title': 'Book Two', 'authors': 'B'},
    }
    with patch.object(client, '_make_request') as mock_req:
        mock_req.side_effect = [
            _Resp([{"id": "shelf-1", "name": "Up Next"}]),
            _Resp([{"id": "b1"}, {"id": "b2"}]),  # minimal records (no title/fileName)
        ]
        with patch.object(client, '_parse_json_response', side_effect=lambda r, _label: r.json()):
            books = client.list_books_on_shelf('Up Next')
    assert len(books) == 2
    assert books[0]['fileName'] == 'one.epub'
    assert books[0]['title'] == 'Book One'
    assert books[1]['fileName'] == 'two.epub'


def test_list_books_on_shelf_passthrough_when_not_in_cache(client):
    """Books not in cache are returned as-is so callers can still see the id."""
    client._book_id_cache = {}
    with patch.object(client, '_make_request') as mock_req:
        mock_req.side_effect = [
            _Resp([{"id": "shelf-1", "name": "Up Next"}]),
            _Resp([{"id": "unknown"}]),
        ]
        with patch.object(client, '_parse_json_response', side_effect=lambda r, _label: r.json()):
            books = client.list_books_on_shelf('Up Next')
    assert len(books) == 1
    assert books[0]['id'] == 'unknown'


def test_list_books_on_shelf_unknown_shelf_returns_empty(client):
    with patch.object(client, '_make_request') as mock_req:
        mock_req.return_value = _Resp([{"id": "shelf-1", "name": "Other"}])
        with patch.object(client, '_parse_json_response', side_effect=lambda r, _label: r.json()):
            books = client.list_books_on_shelf('Up Next')
    assert books == []


def test_list_books_on_shelf_empty_name(client):
    assert client.list_books_on_shelf('') == []


def test_list_books_on_shelf_request_failure_returns_empty(client):
    with patch.object(client, '_make_request') as mock_req:
        mock_req.side_effect = [
            _Resp([{"id": "shelf-1", "name": "Up Next"}]),
            _Resp(None, status_code=500),
        ]
        with patch.object(client, '_parse_json_response', side_effect=lambda r, _label: r.json() if r else None):
            books = client.list_books_on_shelf('Up Next')
    assert books == []


def test_add_to_shelf_uses_default_when_setting_is_blank(client):
    with patch.dict(os.environ, {"BOOKLORE_SHELF_NAME": ""}, clear=False), \
         patch.object(client, 'find_book_by_filename', return_value={'id': 'b1'}), \
         patch.object(client, '_get_or_create_shelf_id', return_value='shelf-1') as resolve_shelf, \
         patch.object(client, '_make_request', return_value=_Resp({}, status_code=204)):
        ok = client.add_to_shelf('book.epub')

    assert ok is True
    resolve_shelf.assert_called_once_with('Kobo')


def test_remove_from_shelf_uses_default_when_setting_is_blank(client):
    with patch.dict(os.environ, {"BOOKLORE_SHELF_NAME": ""}, clear=False), \
         patch.object(client, 'find_book_by_filename', return_value={'id': 'b1'}), \
         patch.object(client, '_make_request') as mock_req, \
         patch.object(client, '_parse_json_response', return_value=[
             {'id': 'shelf-1', 'name': 'Kobo'},
         ]):
        mock_req.side_effect = [_Resp([]), _Resp({}, status_code=204)]
        ok = client.remove_from_shelf('book.epub')

    assert ok is True
    assert mock_req.call_args_list[1].args[2]['shelvesToUnassign'] == ['shelf-1']


# --------------------------------------------------------------------------
# move_between_shelves
# --------------------------------------------------------------------------

def test_move_between_shelves_calls_both_legs(client):
    with patch.object(client, 'remove_from_shelf', return_value=True) as mock_rm, \
         patch.object(client, 'add_to_shelf', return_value=True) as mock_add:
        ok = client.move_between_shelves('book.epub', 'Up Next', 'Kobo')
    assert ok is True
    mock_rm.assert_called_once_with('book.epub', 'Up Next')
    mock_add.assert_called_once_with('book.epub', 'Kobo')


def test_move_between_shelves_remove_failure_short_circuits(client):
    with patch.object(client, 'remove_from_shelf', return_value=False) as mock_rm, \
         patch.object(client, 'add_to_shelf') as mock_add:
        ok = client.move_between_shelves('book.epub', 'Up Next', 'Kobo')
    assert ok is False
    mock_rm.assert_called_once()
    mock_add.assert_not_called()


def test_move_between_shelves_add_failure_returns_false(client):
    with patch.object(client, 'remove_from_shelf', return_value=True), \
         patch.object(client, 'add_to_shelf', return_value=False):
        ok = client.move_between_shelves('book.epub', 'Up Next', 'Kobo')
    assert ok is False


def test_move_between_shelves_same_shelf_noop(client):
    with patch.object(client, 'remove_from_shelf') as mock_rm, \
         patch.object(client, 'add_to_shelf') as mock_add:
        ok = client.move_between_shelves('book.epub', 'Kobo', 'Kobo')
    assert ok is True
    mock_rm.assert_not_called()
    mock_add.assert_not_called()


def test_move_between_shelves_missing_args(client):
    assert client.move_between_shelves('', 'a', 'b') is False
    assert client.move_between_shelves('x', '', 'b') is False
    assert client.move_between_shelves('x', 'a', '') is False
