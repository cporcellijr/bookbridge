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
from tests.base_sync_test import BaseSyncCycleTestCase

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
        c = BookOrbitClient()
        # Pinned to the legacy file-progress route these tests assert (pre-3.6 servers).
        c._reading_progress_api = False
        yield c


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

    with patch.object(client, "_resolve_primary_file_id", return_value=None) as res, \
         patch.object(
             client, "_make_request",
             side_effect=lambda m, e, p=None: captured.update(endpoint=e) or _Resp(status_code=204),
         ):
        ok = client.update_ebook_progress(ebook_primary, 0.5)

    assert ok is True
    res.assert_called_once_with(3, "ebook")
    assert captured["endpoint"] == "/api/v1/books/files/12/progress"


@pytest.mark.parametrize("primary_format", ["epub", "kepub"])
@pytest.mark.parametrize("primary_first", [False, True])
def test_primary_ebook_is_shared_by_cache_read_and_write(client, primary_format, primary_first):
    """#443: advancing the primary ebook must survive the next progress read."""
    primary = {"id": 1, "format": primary_format, "role": "primary"}
    secondary = {"id": 2, "format": "kepub" if primary_format == "epub" else "epub",
                 "role": "content"}
    files = [primary, secondary] if primary_first else [secondary, primary]
    detail = {"id": 443, "title": "Issue 443", "files": files}
    info = client._build_light_info(detail)
    rows = {1: {"fileId": 1, "percentage": 26.0},
            2: {"fileId": 2, "percentage": 0.0}}

    def request(method, endpoint, payload=None):
        if method == "GET":
            return _Resp(list(rows.values()))
        file_id = int(endpoint.split("/")[-2])
        rows[file_id].update(payload)
        return _Resp(status_code=204)

    with patch.object(client, "get_book_detail", return_value=detail), \
         patch.object(client, "_make_request", side_effect=request):
        assert client.get_ebook_progress_rich(443)["pct"] == pytest.approx(0.26)
        assert client.update_ebook_progress(info, 0.488417194869176)
        assert client.get_ebook_progress_rich(443)["pct"] == pytest.approx(0.488417)
    assert info["ebookFileId"] == 1
    assert rows[2]["percentage"] == 0.0


def test_detail_primary_overrides_stale_cached_ebook_id(client):
    """A primary change must not leave writes targeting the old cached ebook."""
    detail = {"files": [{"id": 2, "format": "kepub", "role": "content"},
                        {"id": 1, "format": "epub", "role": "primary"}]}
    with patch.object(client, "get_book_detail", return_value=detail), \
         patch.object(client, "_make_request", return_value=_Resp(status_code=204)) as req:
        assert client.update_ebook_progress({"id": 443, "ebookFileId": 2}, 0.49)
    req.assert_called_once_with("POST", "/api/v1/books/files/1/progress", {"percentage": 49.0})


def test_detail_failure_uses_primary_ebook_from_catalog_cache(client):
    """The detail outage fallback must retain the catalog's primary preference."""
    info = client._build_light_info({"id": 443, "files": [
        {"id": 2, "format": "kepub", "role": "content"},
        {"id": 1, "format": "epub", "role": "primary"},
    ]})
    client._book_cache[443] = info
    with patch.object(client, "get_book_detail", return_value=None), \
         patch.object(client, "_make_request", return_value=_Resp(status_code=204)) as req:
        assert client._resolve_primary_file_id(443, "ebook") == 1
        assert client.update_ebook_progress(info, 0.49)
    req.assert_called_once_with("POST", "/api/v1/books/files/1/progress", {"percentage": 49.0})


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


class TestPrimaryEbookSyncCycle(BaseSyncCycleTestCase):
    """Replay #443 through the sync manager and real BookOrbit transport methods."""

    def setUp(self):
        env = patch.dict(os.environ, {
            "BOOKORBIT_ENABLED": "true", "BOOKORBIT_SERVER": "http://mock",
            "BOOKORBIT_USER": "u", "BOOKORBIT_PASSWORD": "p",
        })
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        self.test_book.ebook_source = "BookOrbit"
        self.test_book.ebook_source_id = "443"

    def get_test_mapping(self):
        return {"abs_id": "issue-443", "abs_title": "Issue 443",
                "ebook_filename": "test-book.epub", "status": "active",
                "transcript_file": os.path.join(self.temp_dir, "transcript.json")}

    def get_test_state_data(self):
        return {"abs": {"pct": 0.0, "ts": 0.0}, "bookorbit": {"pct": 0.0}}

    def get_expected_leader(self):
        return "ABS"

    def get_expected_final_percentage(self):
        return 0.488417194869176

    def get_progress_mock_returns(self):
        return {"abs_progress": {"currentTime": 484.49, "duration": 1000},
                "abs_in_progress": [], "kosync_progress": (0.0, None),
                "storyteller_progress": (0.0, 0.0, None, None),
                "booklore_progress": (0.0, None)}

    def test_primary_epub_advances_and_next_cycle_does_not_repeat_write(self):
        from pathlib import Path
        from unittest.mock import Mock

        from src.sync_clients.abs_sync_client import ABSSyncClient
        from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
        from src.sync_manager import SyncManager

        mocks = self.setup_common_mocks()
        client = BookOrbitClient()
        client._reading_progress_api = False
        client.check_connection = Mock(return_value=True)
        detail = {"id": 443, "title": "Issue 443", "files": [
            {"id": 2, "format": "kepub", "role": "content"},
            {"id": 1, "format": "epub", "role": "primary"},
        ]}
        client._book_cache[443] = client._build_light_info(detail)
        rows = {1: {"fileId": 1, "percentage": 26.0},
                2: {"fileId": 2, "percentage": 0.0}}
        writes = []

        def request(method, endpoint, payload=None):
            if endpoint == "/api/v1/books/443":
                return _Resp(detail)
            if method == "GET":
                return _Resp(list(rows.values()))
            file_id = int(endpoint.split("/")[-2])
            writes.append(file_id)
            rows[file_id].update(payload)
            return _Resp(status_code=204)

        def save_state(state):
            self.test_states[:] = [s for s in self.test_states if s.client_name != state.client_name]
            self.test_states.append(state)

        mocks["database_service"].save_state.side_effect = save_state
        locator = LocatorResult(percentage=self.expected_final_pct,
                                cfi="epubcfi(/6/42!/4/264:0)")
        mocks["ebook_parser"].find_text_location.return_value = locator
        mocks["ebook_parser"].extract_text_and_map.return_value = ("", [])
        mocks["ebook_parser"].get_perfect_ko_xpath.return_value = None
        transcriber = Mock()
        transcriber.get_text_at_time.return_value = "text at the audio position"
        transcriber.find_time_for_text.return_value = 484.49
        manager = SyncManager(
            abs_client=mocks["abs_client"], booklore_client=mocks["booklore_client"],
            bookorbit_client=client,
            transcriber=transcriber, ebook_parser=mocks["ebook_parser"],
            database_service=mocks["database_service"],
            sync_clients={
                "ABS": ABSSyncClient(mocks["abs_client"], transcriber, mocks["ebook_parser"]),
                "BookOrbit": BookOrbitSyncClient(client, mocks["ebook_parser"]),
            },
            data_dir=Path(self.temp_dir), books_dir=Path(self.temp_dir) / "books",
            epub_cache_dir=Path(self.temp_dir) / "epub_cache",
        )
        manager._automatch_hardcover = Mock()
        manager._sync_to_hardcover = Mock()
        manager._flush_reading_sessions = Mock()
        manager._get_local_epub = Mock(return_value=str(Path(self.temp_dir) / "books/test-book.epub"))
        with patch.object(client, "_make_request", side_effect=request), \
             self.assertLogs(level="DEBUG") as logs:
            manager.sync_cycle(target_abs_id="issue-443")
            self.assertEqual(writes, [1])
            self.assertAlmostEqual(client.get_ebook_progress_rich(443)["pct"], 0.488417)
            manager.sync_cycle(target_abs_id="issue-443")
        self.assertEqual(writes, [1])
        self.assertEqual(rows[2]["percentage"], 0.0)
        messages = "\n".join(logs.output)
        self.assertIn("📊 ABS: 0.0000% -> 48.4490%", messages)
        self.assertIn("📊 BookOrbit: 0.0000% -> 26.0000%", messages)
        self.assertIn("BookOrbit: Issue 443 → 48.8% (koreader_xpath=False)", messages)
