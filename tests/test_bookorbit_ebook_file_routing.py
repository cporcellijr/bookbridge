"""Issue #417 — ebook progress must not be written to the audiobook file.

For a BookOrbit book holding both an EPUB and an M4B, BookBridge read ebook
progress from the EPUB file but wrote it to the M4B: `update_ebook_progress`
preferred a cached, format-agnostic `primaryFileId` over the kind-aware
resolver. The reporter's `reading_progress` rows show the write landing on the
audiobook, carrying an EPUB CFI that only the ebook write path emits:

    book_file_id | format | percentage |              cfi              |         updated_at
    -------------+--------+------------+-------------------------------+----------------------------
            2576 | epub   |  72.147125 | epubcfi(/6/38!/4[x9780062...) | 2026-08-28 11:12:53.081+00
           14676 | m4b    |    95.5067 | epubcfi(/6/54!/4/2/6:0)       | 2026-08-28 17:38:29.223+00

Neither of BookOrbit's own "primary file" notions is kind-aware. `books.primary_file_id`
is book-wide and pointed at the m4b for book 480. The file-level `role == "primary"` is
format-agnostic where it exists at all: the reporter's instance constrains
`book_files.role` to content|cover|metadata|supplement so nothing ever matched and file
order decided, while another live instance (measured 2026-08-28) carries `role='primary'`
on every book — an audio format in 57 of 200 sampled. Both routes can name the audiobook
on a book that also holds an EPUB, which is why the id is now resolved per kind.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.bookorbit_client import BookOrbitClient
from src.sync_clients.sync_client_interface import LocatorResult

# The reporter's own book: "Four Nights in May", epub 2576 + m4b 14676. The
# audiobook is listed FIRST, which is what let file order decide the write.
BOOK_ID = 480
EPUB_FILE_ID = 2576
M4B_FILE_ID = 14676
LIST_ROW = {
    "id": BOOK_ID,
    "title": "Four Nights in May",
    "authors": [{"name": "Edward Ashton"}],
    "files": [
        {"id": M4B_FILE_ID, "format": "m4b", "role": "content"},
        {"id": EPUB_FILE_ID, "format": "epub", "role": "content"},
    ],
}


class _Resp:
    def __init__(self, payload=None, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def client():
    with patch.dict(os.environ, {
        "BOOKORBIT_SERVER": "http://mock",
        "BOOKORBIT_USER": "u",
        "BOOKORBIT_PASSWORD": "p",
    }):
        yield BookOrbitClient()


# ---- light cache entry: per-kind file ids ----

def test_light_info_records_both_kinds_file_ids(client):
    info = client._build_light_info(LIST_ROW)
    assert info["ebookFileId"] == EPUB_FILE_ID
    assert info["audioFileId"] == M4B_FILE_ID
    assert info["ebookFormat"] == "epub"
    assert info["audioFormat"] == "m4b"
    assert sorted(info["kinds"]) == ["audiobook", "ebook"]


def test_light_info_file_order_does_not_decide_the_ebook_id(client):
    """The audiobook is first in the list row; the ebook id must still be the epub."""
    reversed_row = dict(LIST_ROW, files=list(reversed(LIST_ROW["files"])))
    assert client._build_light_info(reversed_row)["ebookFileId"] == EPUB_FILE_ID
    assert client._build_light_info(LIST_ROW)["ebookFileId"] == EPUB_FILE_ID


def test_light_info_single_format_book_offers_one_kind(client):
    audio_only = {"id": 9, "title": "A", "files": [{"id": 3, "format": "m4b"}]}
    info = client._build_light_info(audio_only)
    assert info["kinds"] == ["audiobook"]
    assert info["ebookFileId"] is None
    assert info["audioFileId"] == 3
    assert info["primaryFileId"] == 3


# ---- the reported bug: the ebook write landing on the audiobook file ----

def test_ebook_write_targets_the_epub_not_the_audiobook(client):
    captured = {}
    info = client._build_light_info(LIST_ROW)
    locator = LocatorResult(percentage=0.9262, cfi="epubcfi(/6/54!/4/2/6:0)")

    with patch.object(
        client, "_make_request",
        side_effect=lambda m, e, p=None: captured.update(endpoint=e, payload=p)
        or _Resp(status_code=204),
    ):
        ok = client.update_ebook_progress(info, 0.9262, locator)

    assert ok is True
    assert captured["endpoint"] == f"/api/v1/books/files/{EPUB_FILE_ID}/progress"
    assert f"/{M4B_FILE_ID}/" not in captured["endpoint"]
    assert captured["payload"]["cfi"] == "epubcfi(/6/54!/4/2/6:0)"


def test_ebook_write_refuses_a_cached_primary_file_of_the_wrong_format(client):
    """A format-agnostic cached id must not short-circuit the kind-aware resolver."""
    captured = {}
    stale = {"id": BOOK_ID, "primaryFileId": M4B_FILE_ID, "primaryFormat": "m4b"}

    with patch.object(client, "_resolve_primary_file_id", return_value=EPUB_FILE_ID) as res, \
         patch.object(
             client, "_make_request",
             side_effect=lambda m, e, p=None: captured.update(endpoint=e) or _Resp(status_code=204),
         ):
        ok = client.update_ebook_progress(stale, 0.5)

    assert ok is True
    res.assert_called_once_with(BOOK_ID, "ebook")
    assert captured["endpoint"] == f"/api/v1/books/files/{EPUB_FILE_ID}/progress"


def test_ebook_write_accepts_a_cached_primary_file_of_ebook_format(client):
    captured = {}
    ebook_primary = {"id": 3, "primaryFileId": 12, "primaryFormat": "epub"}

    with patch.object(client, "_resolve_primary_file_id") as res, \
         patch.object(
             client, "_make_request",
             side_effect=lambda m, e, p=None: captured.update(endpoint=e) or _Resp(status_code=204),
         ):
        ok = client.update_ebook_progress(ebook_primary, 0.5)

    assert ok is True
    res.assert_not_called()
    assert captured["endpoint"] == "/api/v1/books/files/12/progress"


def test_ebook_write_resolves_from_a_bare_book_dict(client):
    """Sync clients legitimately pass `{"id": book_id}`; the resolver must still run."""
    captured = {}
    with patch.object(client, "_resolve_primary_file_id", return_value=EPUB_FILE_ID), \
         patch.object(
             client, "_make_request",
             side_effect=lambda m, e, p=None: captured.update(endpoint=e) or _Resp(status_code=204),
         ):
        ok = client.update_ebook_progress({"id": BOOK_ID}, 0.5)

    assert ok is True
    assert captured["endpoint"] == f"/api/v1/books/files/{EPUB_FILE_ID}/progress"


def test_ebook_write_refuses_when_no_ebook_file_can_be_resolved(client):
    audio_only = client._build_light_info(
        {"id": 9, "title": "A", "files": [{"id": 3, "format": "m4b"}]}
    )
    with patch.object(client, "_resolve_primary_file_id", return_value=None), \
         patch.object(client, "_make_request") as req:
        ok = client.update_ebook_progress(audio_only, 0.5)

    assert ok is False
    req.assert_not_called()


# ---- the resolver's cache fallback when the detail call fails ----

def _seed_cache(client):
    with client._cache_lock:
        client._book_cache[BOOK_ID] = client._build_light_info(LIST_ROW)


def test_resolver_cache_fallback_is_kind_specific(client):
    _seed_cache(client)
    with patch.object(client, "get_book_detail", return_value=None):
        assert client._resolve_primary_file_id(BOOK_ID, "ebook") == EPUB_FILE_ID
        assert client._resolve_primary_file_id(BOOK_ID, "audiobook") == M4B_FILE_ID


def test_resolver_cache_fallback_returns_none_for_a_kind_the_book_lacks(client):
    with client._cache_lock:
        client._book_cache[9] = client._build_light_info(
            {"id": 9, "title": "A", "files": [{"id": 3, "format": "m4b"}]}
        )
    with patch.object(client, "get_book_detail", return_value=None):
        assert client._resolve_primary_file_id(9, "ebook") is None
        assert client._resolve_primary_file_id(9, "audiobook") == 3


# ---- reads must come from the ebook file's row ----

def test_ebook_read_picks_the_epub_row_over_the_polluted_audio_row(client):
    """Both rows carry an EPUB CFI once the bug has run; only 2576 is ebook progress."""
    rows = [
        {"fileId": EPUB_FILE_ID, "percentage": 72.147125,
         "cfi": "epubcfi(/6/38!/4[x9780062439284]/2,/86/1:79,/98/1:127)"},
        {"fileId": M4B_FILE_ID, "percentage": 95.5067, "cfi": "epubcfi(/6/54!/4/2/6:0)"},
    ]
    with patch.object(client, "_resolve_primary_file_id", return_value=EPUB_FILE_ID), \
         patch.object(client, "_make_request", return_value=_Resp(rows)):
        rich = client.get_ebook_progress_rich(BOOK_ID)

    assert rich["file_id"] == EPUB_FILE_ID
    assert rich["pct"] == pytest.approx(0.72147125)


def test_ebook_read_returns_the_baseline_when_only_the_audio_row_exists(client):
    """An unstarted EPUB is 0.0, never the audiobook file's percentage."""
    rows = [{"fileId": M4B_FILE_ID, "percentage": 95.5067, "cfi": "epubcfi(/6/54!/4/2/6:0)"}]
    with patch.object(client, "_resolve_primary_file_id", return_value=EPUB_FILE_ID), \
         patch.object(client, "_make_request", return_value=_Resp(rows)):
        rich = client.get_ebook_progress_rich(BOOK_ID)

    assert rich["pct"] == 0.0
    assert rich["file_id"] is None
    assert rich["cfi"] is None


def test_ebook_read_keeps_the_single_entry_when_the_ebook_file_is_unknown(client):
    rows = [{"fileId": 2459, "percentage": 42.5, "cfi": "epubcfi(/6/4)"}]
    with patch.object(client, "_resolve_primary_file_id", return_value=None), \
         patch.object(client, "_make_request", return_value=_Resp(rows)):
        rich = client.get_ebook_progress_rich(7)

    assert rich["pct"] == pytest.approx(0.425)
    assert rich["file_id"] == 2459


# ---- a dual-format book belongs to both kind-filtered pools ----

def test_dual_format_book_appears_in_both_pools(client):
    _seed_cache(client)
    client._cache_loaded = True

    assert [b["id"] for b in client.get_all_ebooks()] == [BOOK_ID]
    assert [b["id"] for b in client.search_audiobooks("")] == [BOOK_ID]


def test_kind_membership_falls_back_to_the_scalar_kind(client):
    """Entries built without `kinds` (older shape) keep working."""
    assert client._info_offers_kind({"kind": "ebook"}, "ebook") is True
    assert client._info_offers_kind({"kind": "audiobook"}, "ebook") is False
    assert client._info_offers_kind({"kinds": ["ebook", "audiobook"]}, "audiobook") is True
    assert client._info_offers_kind({"kinds": []}, "ebook") is False
    assert client._info_offers_kind(None, "ebook") is False
