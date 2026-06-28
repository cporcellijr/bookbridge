"""Regression tests for GitHub issue #288 — matching a BookOrbit-hosted ebook.

`get_kosync_id_for_ebook` previously only knew how to fetch ebook bytes from
Grimmory, the local /books disk, ABS, or CWA — never BookOrbit. A user who
configured the BookOrbit API but did not mount the shared /books volume could
not compute the KOReader document hash, so matching failed with a misleading
"Grimmory not configured" warning. These tests pin the BookOrbit download
fallback (by explicit id and by filename resolution).
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import src.web_server as web_server

_EBOOK = "Game Changer - Rachel Reid (2026).epub"


def _clients(bookorbit):
    """A minimal uc() bundle: Grimmory off, BookOrbit injected."""
    booklore = MagicMock()
    booklore.is_configured.return_value = False
    return SimpleNamespace(booklore_client=booklore, bookorbit_client=bookorbit)


def _container(tmp_cache, kosync_id):
    """A container stub whose ebook parser returns a fixed hash for bytes."""
    parser = MagicMock()
    parser.get_kosync_id_from_bytes.return_value = kosync_id
    container = MagicMock()
    container.epub_cache_dir.return_value = Path(tmp_cache)
    container.ebook_parser.return_value = parser
    return container, parser


def test_kosync_id_from_bookorbit_explicit_id():
    """When the match flow passes the BookOrbit book id, download by id (no search)."""
    bookorbit = MagicMock()
    bookorbit.is_configured.return_value = True
    bookorbit.download_book.return_value = b"EPUBBYTES"

    with tempfile.TemporaryDirectory() as tmp:
        container, parser = _container(tmp, "abcd1234")
        with patch.object(web_server, "uc", return_value=_clients(bookorbit)), \
             patch.object(web_server, "container", container), \
             patch.object(web_server, "find_ebook_file", return_value=None):
            result = web_server.get_kosync_id_for_ebook(_EBOOK, bookorbit_id="42")

    assert result == "abcd1234"
    bookorbit.download_book.assert_called_once_with("42")
    bookorbit.find_book_by_filename.assert_not_called()  # explicit id -> no lookup
    parser.get_kosync_id_from_bytes.assert_called_once_with(_EBOOK, b"EPUBBYTES")


def test_kosync_id_from_bookorbit_resolves_by_filename():
    """With no id given, resolve it via a targeted search matching the filename."""
    bookorbit = MagicMock()
    bookorbit.is_configured.return_value = True
    bookorbit.find_book_by_filename.return_value = {"id": "99", "title": "Game Changer"}
    bookorbit.download_book.return_value = b"EPUBBYTES"

    with tempfile.TemporaryDirectory() as tmp:
        container, _ = _container(tmp, "feed0042")
        with patch.object(web_server, "uc", return_value=_clients(bookorbit)), \
             patch.object(web_server, "container", container), \
             patch.object(web_server, "find_ebook_file", return_value=None):
            result = web_server.get_kosync_id_for_ebook(_EBOOK)

    assert result == "feed0042"
    bookorbit.find_book_by_filename.assert_called_once_with(_EBOOK)
    bookorbit.download_book.assert_called_once_with("99")


def test_kosync_id_skips_bookorbit_when_local_file_present():
    """The local /books file still wins — BookOrbit is only a fallback."""
    bookorbit = MagicMock()
    bookorbit.is_configured.return_value = True

    with tempfile.TemporaryDirectory() as tmp:
        container, parser = _container(tmp, "unused")
        parser.get_kosync_id.return_value = "localhash"
        with patch.object(web_server, "uc", return_value=_clients(bookorbit)), \
             patch.object(web_server, "container", container), \
             patch.object(web_server, "find_ebook_file", return_value=Path(tmp) / _EBOOK):
            result = web_server.get_kosync_id_for_ebook(_EBOOK, bookorbit_id="42")

    assert result == "localhash"
    bookorbit.download_book.assert_not_called()


def test_kosync_id_none_when_bookorbit_has_no_match():
    """No local file, BookOrbit configured but the book isn't found -> None."""
    bookorbit = MagicMock()
    bookorbit.is_configured.return_value = True
    bookorbit.find_book_by_filename.return_value = None

    with tempfile.TemporaryDirectory() as tmp:
        container, _ = _container(tmp, "x")
        with patch.object(web_server, "uc", return_value=_clients(bookorbit)), \
             patch.object(web_server, "container", container), \
             patch.object(web_server, "find_ebook_file", return_value=None):
            result = web_server.get_kosync_id_for_ebook(_EBOOK)

    assert result is None
    bookorbit.download_book.assert_not_called()
