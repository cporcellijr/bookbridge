import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.api.booklore_client import BookloreClient, ProgressWriteOutcome
from src.api.bookorbit_client import BookOrbitClient
from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import Book
from src.sync_clients.abs_ebook_sync_client import ABSEbookSyncClient
from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.sync_clients.sync_client_interface import (
    LocatorResult,
    ServiceState,
    SyncResult,
    UpdateProgressRequest,
)
from src.sync_manager import SyncManager
from src.utils.fixed_page_progress import (
    coerce_page,
    count_cbz_pages,
    estimate_cbz_page,
    percentage_from_cbz_page,
)


PAGE_COUNT = 59
TEST_PAGE = 16


@pytest.fixture
def cbz_path(tmp_path):
    path = tmp_path / "comic.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        for page in range(1, PAGE_COUNT + 1):
            archive.writestr(f"pages/{page:03}.jpg", b"image")
        archive.writestr("ComicInfo.xml", b"<ComicInfo />")
        archive.writestr("notes.txt", b"not a page")
    return path


@pytest.fixture
def webp_cbz_path(tmp_path):
    """Match the nested WebP-plus-ComicInfo layout KOReader reads as 58 pages."""
    path = tmp_path / "Suske en Wiske - 002 - De Vliegende Aap.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", b"<ComicInfo />")
        for page in range(1, 59):
            archive.writestr(
                f"Comic Folder\\{page:03}.webp",
                b"webp page",
            )
    return path


class FixedPageParser:
    def __init__(self, path: Path):
        self.path = path
        self._fixed_page_counts = {}
        self.resolve_book_path = MagicMock(return_value=path)
        self.extract_text_and_map = MagicMock(side_effect=AssertionError("CBZ must not use EPUB parsing"))
        self.resolve_xpath = MagicMock(side_effect=AssertionError("CBZ must not resolve EPUB XPath"))
        self.get_text_at_percentage = MagicMock(side_effect=AssertionError("CBZ must not resolve EPUB text"))
        self.get_text_around_cfi = MagicMock(side_effect=AssertionError("CBZ must not hydrate EPUB CFI"))
        self.find_text_location = MagicMock(side_effect=AssertionError("CBZ must not text-match EPUB content"))
        self.get_sentence_level_ko_xpath = MagicMock(side_effect=AssertionError("CBZ must not generate KoSync XPath"))
        self.resolve_xpath_to_index = MagicMock(side_effect=AssertionError("CBZ must not canonicalize EPUB XPath"))

    def get_cached_fixed_page_count(self, filename):
        return self._fixed_page_counts.get(filename)

    def cache_fixed_page_count(self, filename, page_count):
        self._fixed_page_counts[filename] = page_count

    def clear_fixed_page_count_cache(self):
        self._fixed_page_counts.clear()


def cbz_book(path: Path):
    return SimpleNamespace(
        kosync_doc_id="a" * 32,
        original_ebook_filename=str(path),
        ebook_filename=None,
        ebook_source="Grimmory",
        sync_mode="ebook_only",
        abs_id="book-1",
        abs_title="Comic",
    )


def test_cbz_page_count_uses_image_entries_only(cbz_path):
    parser = FixedPageParser(cbz_path)
    assert count_cbz_pages(parser, str(cbz_path)) == PAGE_COUNT
    parser.extract_text_and_map.assert_not_called()


def test_cbz_page_percentage_roundtrip(cbz_path):
    parser = FixedPageParser(cbz_path)
    pct = percentage_from_cbz_page(parser, str(cbz_path), TEST_PAGE)
    assert pct == pytest.approx(TEST_PAGE / PAGE_COUNT)
    assert estimate_cbz_page(parser, str(cbz_path), pct) == TEST_PAGE


def test_koreader_webp_cbz_counts_nested_pages_and_canonicalizes_progress(webp_cbz_path):
    parser = FixedPageParser(webp_cbz_path)

    assert count_cbz_pages(parser, str(webp_cbz_path)) == 58
    assert percentage_from_cbz_page(parser, str(webp_cbz_path), 58) == 1.0
    assert percentage_from_cbz_page(parser, str(webp_cbz_path), 6) == pytest.approx(6 / 58)


def test_kosync_and_grimmory_read_webp_cbz_pages_as_service_state(webp_cbz_path):
    parser = FixedPageParser(webp_cbz_path)
    kosync = KoSyncSyncClient(
        SimpleNamespace(
            get_progress_with_metadata=lambda _doc_id: (0.75, "58", {}),
            is_configured=lambda: True,
        ),
        parser,
    )
    grimmory = BookloreSyncClient(
        SimpleNamespace(
            get_progress_rich=lambda _filename: {
                "pct": 0.0,
                "cfi": None,
                "page": 6,
                "last_read_time": None,
                "status": "READING",
            },
            is_configured=lambda: True,
        ),
        parser,
    )

    kosync_state = kosync.get_service_state(cbz_book(webp_cbz_path), None)
    grimmory_state = grimmory.get_service_state(cbz_book(webp_cbz_path), None)

    assert kosync_state is not None
    assert kosync_state.current["page"] == 58
    assert kosync_state.current["pct"] == 1.0
    assert kosync_state.current["xpath"] == "58"
    assert grimmory_state is not None
    assert grimmory_state.current["page"] == 6
    assert grimmory_state.current["pct"] == pytest.approx(6 / 58)


def test_kosync_cbz_read_uses_concrete_page_as_authoritative(cbz_path):
    parser = FixedPageParser(cbz_path)
    transport = SimpleNamespace(
        get_progress_with_metadata=lambda _doc_id: (1.0, str(TEST_PAGE), {}),
        is_configured=lambda: True,
    )
    client = KoSyncSyncClient(transport, parser)

    state = client.get_service_state(cbz_book(cbz_path), None)

    assert state.current["xpath"] == str(TEST_PAGE)
    assert state.current["pct"] == pytest.approx(TEST_PAGE / PAGE_COUNT)
    parser.extract_text_and_map.assert_not_called()
    parser.resolve_xpath.assert_not_called()


def test_kosync_cbz_write_uses_page_without_epub_xpath_or_cfi(cbz_path):
    parser = FixedPageParser(cbz_path)
    transport = SimpleNamespace(
        update_progress=MagicMock(return_value=True),
        is_configured=lambda: True,
    )
    client = KoSyncSyncClient(transport, parser)
    locator = LocatorResult(
        percentage=1.0,
        page=TEST_PAGE,
    )

    result = client.update_progress(cbz_book(cbz_path), UpdateProgressRequest(locator))

    expected_pct = TEST_PAGE / PAGE_COUNT
    assert result.success is True
    assert result.updated_state == {"pct": pytest.approx(expected_pct), "xpath": str(TEST_PAGE)}
    transport.update_progress.assert_called_once_with("a" * 32, pytest.approx(expected_pct), str(TEST_PAGE))
    parser.get_sentence_level_ko_xpath.assert_not_called()
    parser.resolve_xpath_to_index.assert_not_called()
    parser.extract_text_and_map.assert_not_called()


def test_kosync_cbz_reset_preserves_zero_percent(cbz_path):
    parser = FixedPageParser(cbz_path)
    transport = SimpleNamespace(
        update_progress=MagicMock(return_value=True),
        is_configured=lambda: True,
    )
    client = KoSyncSyncClient(transport, parser)
    locator = LocatorResult(
        percentage=0.0,
        page=TEST_PAGE,
    )

    result = client.update_progress(cbz_book(cbz_path), UpdateProgressRequest(locator))

    assert result.success is True
    assert result.updated_state == {"pct": 0.0, "xpath": "1"}
    transport.update_progress.assert_called_once_with("a" * 32, 0.0, "1")
    parser.extract_text_and_map.assert_not_called()


def test_grimmory_cbx_read_prefers_page_when_percentage_conflicts(cbz_path):
    parser = FixedPageParser(cbz_path)
    grimmory = SimpleNamespace(
        get_progress_rich=lambda _filename: {
            "pct": 1.0,
            "cfi": None,
            "page": TEST_PAGE,
            "last_read_time": None,
            "status": "READING",
        },
        is_configured=lambda: True,
    )
    client = BookloreSyncClient(grimmory, parser)

    state = client.get_service_state(cbz_book(cbz_path), None)

    assert state.current["page"] == TEST_PAGE
    assert state.current["pct"] == pytest.approx(TEST_PAGE / PAGE_COUNT)
    parser.get_text_around_cfi.assert_not_called()
    parser.get_text_at_percentage.assert_not_called()


def make_cbx_api_client(readback):
    book = {
        "id": 42,
        "fileName": "comic.cbz",
        "bookType": "CBX",
        "primaryFile": {
            "id": 77,
            "bookType": "CBX",
            "fileName": "comic.cbz",
        },
    }
    client = BookloreClient.__new__(BookloreClient)
    client.find_book_by_filename = MagicMock(return_value=book)
    client._fetch_and_cache_detail = MagicMock(return_value=None)
    client._make_request = MagicMock(return_value=SimpleNamespace(status_code=204))
    client.get_progress_rich_by_book_id = MagicMock(return_value=readback)
    client._cache_lock = threading.RLock()
    client._book_id_cache = {42: book}
    client._epub_cfi_write_disabled_for_books = set()
    return client


def make_rich_progress_client(data):
    response = SimpleNamespace(status_code=200, json=lambda: data, text="")
    client = BookloreClient.__new__(BookloreClient)
    client._make_request = MagicMock(return_value=response)
    return client


@pytest.mark.parametrize(
    ("book_type", "progress_key"),
    [("EPUB", "epubProgress"), ("PDF", "pdfProgress")],
)
def test_grimmory_non_cbx_missing_progress_preserves_zero_semantics(
    book_type, progress_key
):
    client = make_rich_progress_client(
        {"id": 42, "bookType": book_type, progress_key: None}
    )

    rich = client.get_progress_rich_by_book_id(42)

    assert rich["pct"] == 0.0
    assert rich["percentage_present"] is False


def test_grimmory_unknown_book_type_preserves_safe_zero_semantics():
    client = make_rich_progress_client({"id": 42, "bookType": "UNKNOWN"})

    assert client.get_progress_rich_by_book_id(42)["pct"] == 0.0


def test_untouched_grimmory_cbx_preserves_zero_percent_state():
    client = make_rich_progress_client(
        {
            "id": 42,
            "bookType": "CBX",
            "cbxProgress": None,
            "primaryFile": {"id": 77, "bookType": "CBX"},
        }
    )

    rich = client.get_progress_rich_by_book_id(42)

    assert rich["pct"] == 0.0
    assert rich["page"] is None
    assert rich["percentage_present"] is False


def test_untouched_grimmory_cbz_produces_zero_percent_service_state(cbz_path):
    parser = FixedPageParser(cbz_path)
    api = SimpleNamespace(
        get_progress_rich=lambda _filename: {
            "pct": 0.0,
            "percentage_present": False,
            "cfi": None,
            "page": None,
            "last_read_time": None,
            "status": None,
        },
        is_configured=lambda: True,
    )

    state = BookloreSyncClient(api, parser).get_service_state(
        cbz_book(cbz_path), None
    )

    assert state is not None
    assert state.current["pct"] == 0.0
    assert state.current.get("page") is None


def test_grimmory_epub_missing_progress_produces_service_state():
    api = SimpleNamespace(
        get_progress_rich=lambda _filename: {
            "pct": 0.0,
            "percentage_present": False,
            "cfi": None,
            "page": None,
            "last_read_time": None,
            "status": None,
        },
        is_configured=lambda: True,
    )
    parser = MagicMock()
    client = BookloreSyncClient(api, parser)
    book = cbz_book(Path("book.epub"))
    book.original_ebook_filename = "book.epub"

    state = client.get_service_state(book, None)

    assert state is not None
    assert state.current["pct"] == 0.0


def test_grimmory_cbx_write_sends_native_and_file_progress():
    expected_pct = TEST_PAGE / PAGE_COUNT
    client = make_cbx_api_client({"pct": expected_pct, "page": TEST_PAGE})
    locator = LocatorResult(percentage=expected_pct, page=TEST_PAGE)

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress("comic.cbz", expected_pct, locator) is True

    payload = client._make_request.call_args.args[2]
    assert payload["cbxProgress"] == {
        "page": TEST_PAGE,
        "percentage": pytest.approx(expected_pct * 100.0),
    }
    assert payload["fileProgress"] == {
        "bookFileId": 77,
        "positionData": str(TEST_PAGE),
        "progressPercent": pytest.approx(expected_pct * 100.0),
    }
    assert client.get_progress_rich_by_book_id.call_count == 2


def test_grimmory_cbx_reset_writes_page_one_and_zero_percent():
    client = make_cbx_api_client({"pct": 0.0, "page": 1})
    locator = LocatorResult(percentage=0.0)

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress("comic.cbz", 0.0, locator) is True

    payload = client._make_request.call_args.args[2]
    assert payload["cbxProgress"] == {"page": 1, "percentage": 0.0}
    assert payload["fileProgress"] == {
        "bookFileId": 77,
        "positionData": "1",
        "progressPercent": 0.0,
    }


def test_grimmory_sync_client_reset_stays_zero_percent(cbz_path):
    parser = FixedPageParser(cbz_path)
    api = MagicMock()
    api.update_progress.return_value = True
    client = BookloreSyncClient(api, parser)

    result = client.update_progress(
        cbz_book(cbz_path),
        UpdateProgressRequest(LocatorResult(percentage=0.0, page=1)),
    )

    assert result.success is True
    assert result.updated_state == {"pct": 0.0, "page": 1}
    written_locator = api.update_progress.call_args.args[2]
    assert api.update_progress.call_args.args[1] == 0.0
    assert written_locator.percentage == 0.0
    assert written_locator.page == 1


def test_grimmory_cbx_write_accepts_correct_page_with_percentage_mismatch():
    expected_pct = TEST_PAGE / PAGE_COUNT
    client = make_cbx_api_client({"pct": 1.0, "page": TEST_PAGE})
    locator = LocatorResult(percentage=expected_pct, page=TEST_PAGE)

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress("comic.cbz", expected_pct, locator) is True

    assert client.get_progress_rich_by_book_id.call_count == 2


def build_manager(tmp_path):
    db = MagicMock()
    db.get_books_by_status.return_value = []
    return SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=None,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )


def cycle_client(*, fixed_pages=False, update_result=None):
    client = MagicMock()
    client.get_supported_sync_types.return_value = {"ebook"}
    client.supports_book.return_value = True
    client.can_be_leader.return_value = True
    client.supports_fixed_page_progress.return_value = fixed_pages
    client.get_text_from_current_state.return_value = "anchor text"
    client.get_locator_from_text.side_effect = lambda _txt, _epub, pct: LocatorResult(
        percentage=pct, cfi="epubcfi(/6/2)", match_index=40
    )
    client.update_progress.return_value = update_result or SyncResult(
        0.4, True, {"pct": 0.4}
    )
    return client


def cycle_state(pct, *, page=None, normalized_ts=None):
    current = {"pct": pct}
    if page is not None:
        current["page"] = page
    if normalized_ts is not None:
        current["_normalized_ts"] = normalized_ts
    return ServiceState(
        current=current,
        previous_pct=pct,
        delta=0.0,
        threshold=0.01,
        is_configured=True,
        display=("test", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda value: f"{value:.2%}",
    )


def configure_cycle(manager, book, config, previous_states):
    manager.sync_delta_between_clients = 0.005
    manager.delta_chars_thresh = 2000
    manager.database_service.get_book.return_value = book
    manager.database_service.get_states_for_book.return_value = previous_states
    manager.database_service.save_state = MagicMock()
    manager._filter_books_for_current_user = lambda books, _bulk: books
    manager._promote_alignment_backed_book = MagicMock(return_value=False)
    manager._fetch_states_parallel = MagicMock(return_value=config)
    manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)
    manager._record_reading_movement = MagicMock()
    manager._get_local_epub = lambda filename: Path(filename)


def test_new_zero_percent_grimmory_state_catches_up_from_kosync(tmp_path):
    manager = build_manager(tmp_path)
    kosync = cycle_client()
    grimmory = cycle_client()
    manager.sync_clients = {"KoSync": kosync, "BookLore": grimmory}
    book = SimpleNamespace(
        abs_id="book-1",
        abs_title="Initial Catch-up",
        status="active",
        sync_mode="ebook_only",
        ebook_filename="book.epub",
        original_ebook_filename="book.epub",
        duration=None,
        transcript_file=None,
    )
    config = {
        "KoSync": cycle_state(0.4),
        "BookLore": cycle_state(0.0),
    }
    previous = [
        SimpleNamespace(
            client_name="kosync",
            last_updated=100,
            percentage=0.4,
            timestamp=None,
            xpath=None,
            cfi=None,
        )
    ]
    configure_cycle(manager, book, config, previous)

    manager._sync_cycle_internal(target_abs_id="book-1")

    grimmory.update_progress.assert_called_once()
    request = grimmory.update_progress.call_args.args[1]
    assert request.locator_result.percentage == 0.4


def test_real_cbz_clients_catch_up_untouched_grimmory_in_manager_cycle(
    tmp_path, cbz_path
):
    manager = build_manager(tmp_path)
    parser = FixedPageParser(cbz_path)
    kosync_api = SimpleNamespace(
        get_progress_with_metadata=lambda _doc_id: (
            TEST_PAGE / PAGE_COUNT,
            str(TEST_PAGE),
            {},
        ),
        update_progress=MagicMock(return_value=True),
        is_configured=lambda: True,
    )
    grimmory_api = SimpleNamespace(
        get_progress_rich=lambda _filename: {
            "pct": 0.0,
            "percentage_present": False,
            "cfi": None,
            "page": None,
            "last_read_time": None,
            "status": None,
        },
        update_progress=MagicMock(return_value=True),
        is_configured=lambda: True,
    )
    manager.ebook_parser = parser
    manager.sync_clients = {
        "KoSync": KoSyncSyncClient(kosync_api, parser),
        "BookLore": BookloreSyncClient(grimmory_api, parser),
    }
    book = cbz_book(cbz_path)
    book.status = "active"
    book.duration = None
    book.transcript_file = None
    book.audio_source = None
    manager.database_service.get_book.return_value = book
    manager.database_service.get_states_for_book.return_value = [
        SimpleNamespace(
            client_name="kosync",
            last_updated=100,
            percentage=TEST_PAGE / PAGE_COUNT,
            timestamp=None,
            xpath=str(TEST_PAGE),
            cfi=None,
            locator_json=f'{{"page": {TEST_PAGE}}}',
        )
    ]
    manager.database_service.save_state = MagicMock()
    manager._filter_books_for_current_user = lambda books, _bulk: books
    manager._promote_alignment_backed_book = MagicMock(return_value=False)
    manager._normalize_for_cross_format_comparison = MagicMock(return_value=None)
    manager._record_reading_movement = MagicMock()
    manager._get_local_epub = MagicMock(return_value=cbz_path)

    manager._sync_cycle_internal(target_abs_id="book-1")

    grimmory_api.update_progress.assert_called_once()
    filename, percentage, locator = grimmory_api.update_progress.call_args.args
    assert filename == str(cbz_path)
    assert percentage == pytest.approx(TEST_PAGE / PAGE_COUNT)
    assert locator.page == TEST_PAGE


def test_fixed_page_skip_is_persisted_by_manager_without_write_marker(tmp_path):
    manager = build_manager(tmp_path)
    kosync = cycle_client(fixed_pages=True)
    skipped_state = {"pct": 0.0, "page": 1}
    grimmory = cycle_client(
        fixed_pages=True,
        update_result=SyncResult(0.0, True, skipped_state, skipped=True),
    )
    manager.sync_clients = {"KoSync": kosync, "BookLore": grimmory}
    book = SimpleNamespace(
        abs_id="book-1",
        abs_title="Skip Persistence",
        status="active",
        sync_mode="ebook_only",
        ebook_filename="comic.cbz",
        original_ebook_filename="comic.cbz",
        duration=None,
        transcript_file=None,
    )
    config = {
        "KoSync": cycle_state(0.4, page=16),
        "BookLore": cycle_state(0.0, page=1),
    }
    previous = [
        SimpleNamespace(
            client_name="kosync", last_updated=100, percentage=0.4,
            timestamp=None, xpath="16", cfi=None, locator_json='{"page": 16}',
        )
    ]
    configure_cycle(manager, book, config, previous)

    with patch("src.services.write_tracker.record_write") as record_write:
        manager._sync_cycle_internal(target_abs_id="book-1")

    saved_followers = [
        state for call in manager.database_service.save_state.call_args_list
        for state in call.args
        if state.client_name == "booklore"
    ]
    assert len(saved_followers) == 1
    assert saved_followers[0].percentage == 0.0
    assert saved_followers[0].locator_json == '{"page": 1}'
    record_write.assert_not_called()
    assert SyncManager._sync_result_was_applied(
        grimmory.update_progress.return_value
    ) is False


def test_cbz_with_alignment_metadata_never_enters_locator_resolution(tmp_path):
    manager = build_manager(tmp_path)
    kosync = cycle_client(fixed_pages=True)
    grimmory = cycle_client(fixed_pages=True)
    manager.sync_clients = {"KoSync": kosync, "BookLore": grimmory}
    book = SimpleNamespace(
        abs_id="book-1",
        abs_title="Comic with transcript",
        status="active",
        sync_mode="ebook_only",
        ebook_filename="fallback.epub",
        original_ebook_filename="comic.cbz",
        duration=3600,
        transcript_file="DB_MANAGED",
    )
    config = {
        "KoSync": cycle_state(0.4, page=16, normalized_ts=1440),
        "BookLore": cycle_state(0.0, page=1),
    }
    configure_cycle(manager, book, config, [])
    manager._resolve_alignment_locator_from_abs_timestamp = MagicMock(
        side_effect=AssertionError("CBZ must not use alignment locator resolution")
    )
    manager._resolve_storyteller_locator_from_abs_timestamp = MagicMock(
        side_effect=AssertionError("CBZ must not use Storyteller locator resolution")
    )
    manager._get_local_epub = MagicMock(
        side_effect=lambda filename: (
            None if str(filename).endswith("comic.cbz") else Path(filename)
        )
    )

    manager._sync_cycle_internal(target_abs_id="book-1")

    manager._resolve_alignment_locator_from_abs_timestamp.assert_not_called()
    manager._resolve_storyteller_locator_from_abs_timestamp.assert_not_called()
    request = grimmory.update_progress.call_args.args[1]
    assert request.locator_result.page == TEST_PAGE


def test_sync_manager_never_parses_or_hydrates_cbz_as_epub(tmp_path):
    manager = build_manager(tmp_path)
    manager._get_local_epub = MagicMock(side_effect=AssertionError("CBZ must not be hydrated as EPUB"))
    manager.ebook_parser.extract_text_and_map.side_effect = AssertionError("CBZ must not use EPUB parser")

    assert manager._get_cached_ebook_text("comic.cbz") == (None, 0)
    locator = LocatorResult(percentage=0.5)
    assert manager._hydrate_cfi_locator(locator, "comic.cbz", "book-1", "Comic", "KoSync", 0.5, str) is None

    manager._get_local_epub.assert_not_called()
    manager.ebook_parser.extract_text_and_map.assert_not_called()


def test_sync_manager_cbz_background_prep_skips_epub_warmup(tmp_path, cbz_path):
    manager = build_manager(tmp_path)
    library = MagicMock()
    library.acquire_ebook.return_value = str(cbz_path)
    manager.ebook_parser.extract_text_and_map.side_effect = AssertionError("CBZ must not use EPUB parser")
    job = SimpleNamespace(retry_count=2, last_error="previous failure", progress=0.4)
    manager.database_service.get_latest_job.return_value = job
    book = Book(
        abs_id="cbz-background-prep",
        abs_title="Comic",
        ebook_filename=cbz_path.name,
        original_ebook_filename=cbz_path.name,
        kosync_doc_id="a" * 32,
        sync_mode="ebook_only",
        status="processing",
    )

    manager._run_background_job(book, library_service=library)

    library.acquire_ebook.assert_called_once_with(None, book)
    manager.ebook_parser.extract_text_and_map.assert_not_called()
    assert book.status == "active"
    manager.database_service.update_book_if_exists.assert_called_once_with(book)
    manager.database_service.save_job.assert_called_once_with(job)
    assert job.progress == 1.0
    assert job.last_error is None
    assert job.retry_count == 0


def test_grimmory_nonzero_cbz_write_without_page_is_rejected():
    client = make_cbx_api_client({"pct": 0.7, "page": None})

    assert client.update_progress("comic.cbz", 0.6949, LocatorResult(percentage=0.6949)) is False

    client._make_request.assert_not_called()


@pytest.mark.parametrize("suffix", [".cbr", ".cbt", ".cb7"])
def test_non_cbz_cbx_formats_keep_percentage_only_file_progress(suffix):
    client = make_cbx_api_client({"pct": 0.5, "page": 1})

    assert client.update_progress(f"comic{suffix}", 0.5, LocatorResult(percentage=0.5)) is True

    payload = client._make_request.call_args.args[2]
    assert payload == {
        "bookId": 42,
        "fileProgress": {"bookFileId": 77, "progressPercent": 50.0},
    }


def test_kosync_page_only_read_derives_percentage(cbz_path):
    parser = FixedPageParser(cbz_path)
    transport = SimpleNamespace(
        get_progress_with_metadata=lambda _doc_id: (None, str(TEST_PAGE), {}),
        is_configured=lambda: True,
    )

    state = KoSyncSyncClient(transport, parser).get_service_state(cbz_book(cbz_path), None)

    assert state.current["page"] == TEST_PAGE
    assert state.current["pct"] == pytest.approx(TEST_PAGE / PAGE_COUNT)


@pytest.mark.parametrize("reported_pct", [None, 0.0])
def test_grimmory_concrete_page_beats_missing_or_stale_zero(cbz_path, reported_pct):
    parser = FixedPageParser(cbz_path)
    grimmory = SimpleNamespace(
        get_progress_rich=lambda _filename: {
            "pct": reported_pct,
            "percentage_present": reported_pct is not None,
            "cfi": None,
            "page": TEST_PAGE,
            "last_read_time": None,
            "status": "READING",
        },
        is_configured=lambda: True,
    )

    state = BookloreSyncClient(grimmory, parser).get_service_state(cbz_book(cbz_path), None)

    assert state.current["page"] == TEST_PAGE
    assert state.current["pct"] == pytest.approx(TEST_PAGE / PAGE_COUNT)


def test_grimmory_applied_write_with_inconclusive_readback_succeeds():
    expected_pct = TEST_PAGE / PAGE_COUNT
    client = make_cbx_api_client(None)

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress(
            "comic.cbz", expected_pct, LocatorResult(percentage=expected_pct, page=TEST_PAGE)
        ) is True

    assert client.get_progress_rich_by_book_id.call_count == 7
    assert client._make_request.call_count == 3


def test_grimmory_sync_records_an_applied_cbz_write(cbz_path):
    parser = FixedPageParser(cbz_path)
    api = MagicMock()
    api.update_progress.return_value = True
    client = BookloreSyncClient(api, parser)
    pct = TEST_PAGE / PAGE_COUNT

    with patch("src.services.write_tracker.record_write") as record_write:
        result = client.update_progress(
            cbz_book(cbz_path),
            UpdateProgressRequest(LocatorResult(percentage=pct, page=TEST_PAGE)),
        )

    assert result.success is True
    record_write.assert_called_once_with("BookLore", "book-1", pytest.approx(pct))


def test_grimmory_cbz_without_primary_file_id_degrades_to_legacy_only():
    expected_pct = TEST_PAGE / PAGE_COUNT
    client = make_cbx_api_client({"pct": expected_pct, "page": TEST_PAGE})
    client.find_book_by_filename.return_value["primaryFile"].pop("id")

    assert client.update_progress(
        "comic.cbz", expected_pct, LocatorResult(percentage=expected_pct, page=TEST_PAGE)
    ) is True

    payload = client._make_request.call_args.args[2]
    assert "fileProgress" not in payload
    assert payload["cbxProgress"]["page"] == TEST_PAGE


def test_grimmory_combined_payload_has_safe_file_progress_fallback():
    expected_pct = TEST_PAGE / PAGE_COUNT
    client = make_cbx_api_client({"pct": expected_pct, "page": TEST_PAGE})
    failed = SimpleNamespace(status_code=400, text="unsupported")
    applied = SimpleNamespace(status_code=204, text="")
    client._make_request.side_effect = [failed, applied]

    assert client.update_progress(
        "comic.cbz", expected_pct, LocatorResult(percentage=expected_pct, page=TEST_PAGE)
    ) is True

    payloads = [call.args[2] for call in client._make_request.call_args_list]
    assert set(payloads[0]) == {"bookId", "cbxProgress", "fileProgress"}
    assert set(payloads[1]) == {"bookId", "fileProgress"}
    assert payloads[1]["fileProgress"]["positionData"] == str(TEST_PAGE)


def test_grimmory_stale_file_progress_after_combined_post_uses_fallback():
    expected_pct = TEST_PAGE / PAGE_COUNT
    before = {
        "pct": 10 / PAGE_COUNT,
        "page": 10,
        "cbx_page": 10,
        "file_page": 10,
    }
    stale_file = {
        "pct": expected_pct,
        "page": TEST_PAGE,
        "cbx_page": TEST_PAGE,
        "file_page": 10,
    }
    fully_applied = {
        "pct": expected_pct,
        "page": TEST_PAGE,
        "cbx_page": TEST_PAGE,
        "file_page": TEST_PAGE,
    }
    client = make_cbx_api_client(None)
    client.get_progress_rich_by_book_id.side_effect = [
        before,
        stale_file,
        stale_file,
        fully_applied,
    ]

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress(
            "comic.cbz",
            expected_pct,
            LocatorResult(percentage=expected_pct, page=TEST_PAGE),
        ) is True

    assert client._make_request.call_count == 2
    fallback = client._make_request.call_args_list[1].args[2]
    assert set(fallback) == {"bookId", "fileProgress"}
    assert fallback["fileProgress"]["positionData"] == str(TEST_PAGE)


def test_grimmory_failed_prewrite_read_does_not_mask_non_applied_write():
    expected_pct = TEST_PAGE / PAGE_COUNT
    unchanged = {
        "pct": 10 / PAGE_COUNT,
        "page": 10,
        "cbx_page": 10,
        "file_page": 10,
    }
    applied = {
        "pct": expected_pct,
        "page": TEST_PAGE,
        "cbx_page": TEST_PAGE,
        "file_page": TEST_PAGE,
    }
    client = make_cbx_api_client(None)
    client.get_progress_rich_by_book_id.side_effect = [
        None,
        unchanged,
        unchanged,
        applied,
    ]

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress(
            "comic.cbz",
            expected_pct,
            LocatorResult(percentage=expected_pct, page=TEST_PAGE),
        ) is True

    assert client._make_request.call_count == 2
    assert set(client._make_request.call_args_list[1].args[2]) == {
        "bookId",
        "fileProgress",
    }


def test_grimmory_page_less_file_row_degrades_to_cbx_verification():
    expected_pct = TEST_PAGE / PAGE_COUNT
    observed = {
        "pct": expected_pct,
        "page": TEST_PAGE,
        "cbx_page": TEST_PAGE,
        "file_page": None,
        "file_page_observable": False,
    }
    client = make_cbx_api_client(observed)

    assert client.update_progress(
        "comic.cbz",
        expected_pct,
        LocatorResult(percentage=expected_pct, page=TEST_PAGE),
    ) is True

    assert client._make_request.call_count == 1


def test_rich_progress_marks_page_less_file_row_unobservable():
    client = make_rich_progress_client(
        {
            "id": 42,
            "bookType": "CBX",
            "cbxProgress": {"page": TEST_PAGE, "percentage": 27.12},
            "primaryFile": {
                "id": 77,
                "bookType": "CBX",
                "fileProgress": {"progressPercent": 27.12},
            },
        }
    )

    rich = client.get_progress_rich_by_book_id(42)

    assert rich["file_page"] is None
    assert rich["file_page_observable"] is False


def test_rich_progress_uses_page_when_position_data_is_explicitly_null():
    client = make_rich_progress_client(
        {
            "id": 42,
            "bookType": "CBX",
            "cbxProgress": {"page": TEST_PAGE, "percentage": 27.12},
            "primaryFile": {
                "id": 77,
                "bookType": "CBX",
                "fileProgress": {
                    "positionData": None,
                    "page": TEST_PAGE,
                    "progressPercent": 27.12,
                },
            },
        }
    )

    rich = client.get_progress_rich_by_book_id(42)

    assert rich["file_page"] == TEST_PAGE
    assert rich["file_page_observable"] is True


def test_grimmory_concurrent_remote_page_is_preserved_in_cache():
    expected_pct = TEST_PAGE / PAGE_COUNT
    before = {"pct": 10 / PAGE_COUNT, "page": 10, "cbx_page": 10, "file_page": 10}
    moved = {"pct": 20 / PAGE_COUNT, "page": 20, "cbx_page": 20, "file_page": 20}
    client = make_cbx_api_client(None)
    client.get_progress_rich_by_book_id.side_effect = [before, moved, moved]

    with patch("src.api.booklore_client.time.sleep"):
        assert client.update_progress(
            "comic.cbz",
            expected_pct,
            LocatorResult(percentage=expected_pct, page=TEST_PAGE),
        ) is True

    assert client._book_id_cache[42]["cbxProgress"]["page"] == 20
    assert client._book_id_cache[42]["cbxProgress"]["page"] != TEST_PAGE


def test_page_count_matches_koreader_mupdf_extension_filter(tmp_path):
    accepted = [
        ".bmp", ".gif", ".hdp", ".j2k", ".jb2", ".jbig2", ".jp2",
        ".jpeg", ".jpg", ".jpx", ".jxr", ".pam", ".pbm", ".pgm",
        ".pkm", ".png", ".pnm", ".ppm", ".tif", ".tiff", ".wdp", ".webp",
    ]
    path = tmp_path / "extensions.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        for index, suffix in enumerate(accepted):
            archive.writestr(f"nested/{index}{suffix.upper()}", b"page")
        archive.writestr("cover.avif", b"not a KOReader MuPDF CBZ page")
        archive.writestr("__MACOSX/._001.jpg", b"still counted")

    assert count_cbz_pages(FixedPageParser(path), str(path)) == len(accepted) + 1


@pytest.mark.parametrize("kind", ["empty", "corrupt", "missing"])
def test_invalid_cbz_has_no_invented_page_count(tmp_path, kind):
    path = tmp_path / f"{kind}.cbz"
    if kind == "empty":
        with zipfile.ZipFile(path, "w"):
            pass
    elif kind == "corrupt":
        path.write_bytes(b"not a zip")
    parser = FixedPageParser(path)
    if kind == "missing":
        parser.resolve_book_path.return_value = None

    assert count_cbz_pages(parser, str(path)) is None


def test_transient_page_count_failure_is_not_cached(cbz_path):
    parser = FixedPageParser(cbz_path)
    parser.resolve_book_path.side_effect = [OSError("temporary NAS failure"), cbz_path]

    assert count_cbz_pages(parser, str(cbz_path)) is None
    assert count_cbz_pages(parser, str(cbz_path)) == PAGE_COUNT
    assert parser.resolve_book_path.call_count == 2


def test_percentage_page_inverse_and_transition_intervals(cbz_path):
    parser = FixedPageParser(cbz_path)
    for page in range(1, PAGE_COUNT + 1):
        pct = percentage_from_cbz_page(parser, str(cbz_path), page)
        assert estimate_cbz_page(parser, str(cbz_path), pct) == page
        if page > 1:
            just_after_previous = ((page - 1) / PAGE_COUNT) + 1e-9
            assert estimate_cbz_page(
                parser, str(cbz_path), just_after_previous, provenance="kosync"
            ) == page


def test_generic_rounded_percentage_uses_nearest_page(cbz_path):
    parser = FixedPageParser(cbz_path)

    assert estimate_cbz_page(parser, str(cbz_path), 0.2712) == TEST_PAGE


def test_koreader_truncated_percentage_uses_strict_interval(cbz_path):
    parser = FixedPageParser(cbz_path)
    truncated = int((TEST_PAGE / PAGE_COUNT) * 10_000) / 10_000

    assert estimate_cbz_page(
        parser, str(cbz_path), truncated, provenance="kosync"
    ) == TEST_PAGE


@pytest.mark.parametrize("page", [0, -1, PAGE_COUNT + 1, 1.5, float("inf")])
def test_out_of_range_or_invalid_page_has_no_percentage(cbz_path, page):
    parser = FixedPageParser(cbz_path)
    assert percentage_from_cbz_page(parser, str(cbz_path), page) is None


def test_bare_filename_is_resolved_instead_of_trusted_from_cwd(tmp_path, monkeypatch):
    cwd_copy = tmp_path / "comic.cbz"
    with zipfile.ZipFile(cwd_copy, "w") as archive:
        archive.writestr("one.jpg", b"page")
    resolved = tmp_path / "library.cbz"
    with zipfile.ZipFile(resolved, "w") as archive:
        archive.writestr("one.jpg", b"page")
        archive.writestr("two.jpg", b"page")
    parser = FixedPageParser(resolved)
    monkeypatch.chdir(tmp_path)

    assert count_cbz_pages(parser, "comic.cbz") == 2
    parser.resolve_book_path.assert_called_once_with("comic.cbz")


def test_page_count_uses_per_cycle_cache(cbz_path):
    parser = FixedPageParser(cbz_path)

    assert count_cbz_pages(parser, str(cbz_path)) == PAGE_COUNT
    assert count_cbz_pages(parser, str(cbz_path)) == PAGE_COUNT
    parser.resolve_book_path.assert_called_once_with(str(cbz_path))


def test_grimmory_page_with_unavailable_count_never_propagates_stale_percentage(tmp_path):
    missing = tmp_path / "missing.cbz"
    parser = FixedPageParser(missing)
    parser.resolve_book_path.return_value = None
    grimmory = SimpleNamespace(
        get_progress_rich=lambda _filename: {
            "pct": 1.0,
            "percentage_present": True,
            "cfi": None,
            "page": TEST_PAGE,
            "last_read_time": None,
            "status": "READING",
        },
        is_configured=lambda: True,
    )

    state = BookloreSyncClient(grimmory, parser).get_service_state(
        cbz_book(missing), None
    )

    assert state is None


@pytest.mark.parametrize("remote_page", [PAGE_COUNT + 1, 16.7])
def test_out_of_range_or_fractional_page_is_rejected_on_read(cbz_path, remote_page):
    parser = FixedPageParser(cbz_path)
    transport = SimpleNamespace(
        get_progress_with_metadata=lambda _doc_id: (1.0, str(remote_page), {}),
        is_configured=lambda: True,
    )

    assert KoSyncSyncClient(transport, parser).get_service_state(
        cbz_book(cbz_path), None
    ) is None


def test_fixed_page_policy_skip_is_successful_and_retains_observed_state(cbz_path):
    parser = FixedPageParser(cbz_path)
    parser.resolve_book_path.return_value = None
    transport = SimpleNamespace(
        update_progress=MagicMock(return_value=True),
        is_configured=lambda: True,
    )
    observed = SimpleNamespace(current={"pct": 0.25, "page": 15, "xpath": "15"})
    request = UpdateProgressRequest(
        LocatorResult(percentage=0.5, page=TEST_PAGE), current_state=observed
    )

    result = KoSyncSyncClient(transport, parser).update_progress(
        cbz_book(cbz_path), request
    )

    assert result.success is True
    assert result.skipped is True
    assert result.updated_state == observed.current
    assert SyncManager._sync_result_was_applied(result) is False
    transport.update_progress.assert_not_called()


def test_single_fixed_page_turn_is_significant_below_percentage_threshold():
    manager = SyncManager.__new__(SyncManager)
    fixed_client = MagicMock()
    fixed_client.supports_fixed_page_progress.return_value = True
    manager.sync_clients = {"KoSync": fixed_client}
    state = SimpleNamespace(
        current={"pct": 2 / 401, "page": 2, "_previous_page": 1},
        previous_pct=1 / 401,
    )
    book = SimpleNamespace(
        original_ebook_filename="long-comic.cbz", ebook_filename=None, duration=None
    )

    assert manager._has_significant_delta("KoSync", {"KoSync": state}, book) is True


def test_estimated_page_does_not_trigger_page_delta_significance():
    manager = SyncManager.__new__(SyncManager)
    fixed_client = MagicMock()
    fixed_client.supports_fixed_page_progress.return_value = True
    manager.sync_clients = {"KoSync": fixed_client}
    state = SimpleNamespace(
        current={
            "pct": 2 / 401,
            "page": 2,
            "_previous_page": 1,
            "_page_is_estimated": True,
        },
        previous_pct=1 / 401,
    )
    book = SimpleNamespace(
        original_ebook_filename="long-comic.cbz", ebook_filename=None, duration=None
    )

    assert manager._has_fixed_page_delta("KoSync", state, book) is False


@pytest.mark.parametrize("suffix", [".cbr", ".cbt", ".cb7"])
def test_unsupported_cbx_without_primary_file_id_is_safe_skip(suffix):
    client = make_cbx_api_client({"pct": 0.5, "page": None})
    client.find_book_by_filename.return_value["primaryFile"].pop("id")

    assert client.update_progress(
        f"comic{suffix}", 0.5, LocatorResult(percentage=0.5)
    ) is ProgressWriteOutcome.SKIPPED
    client._make_request.assert_not_called()


@pytest.mark.parametrize("suffix", [".cbr", ".cbt", ".cb7"])
def test_unsupported_cbx_skip_uses_sync_result_contract(suffix):
    api = make_cbx_api_client({"pct": 0.5, "page": None})
    api.find_book_by_filename.return_value["primaryFile"].pop("id")
    parser = MagicMock()
    book = cbz_book(Path(f"comic{suffix}"))
    observed = cycle_state(0.25)

    result = BookloreSyncClient(api, parser).update_progress(
        book,
        UpdateProgressRequest(
            LocatorResult(percentage=0.5), current_state=observed
        ),
    )

    assert result.success is True
    assert result.skipped is True
    assert result.updated_state == observed.current
    api._make_request.assert_not_called()


@pytest.mark.parametrize("client_name", ["ABS Ebook", "BookOrbit", "Storyteller"])
def test_non_fixed_page_clients_never_receive_a_page_as_fake_cfi(client_name):
    client = MagicMock(name=client_name)
    client.supports_fixed_page_progress.return_value = False
    state = SimpleNamespace(current={"pct": 0.5, "page": TEST_PAGE})

    locator = SyncManager._fixed_page_locator_from_state(client, state, 0.5)

    assert locator.page is None
    assert locator.cfi is None
    assert "bookbridge" not in repr(locator)


def test_non_cbz_aware_leader_cannot_invent_cbz_page():
    client = MagicMock()
    client.supports_fixed_page_progress.return_value = False
    state = SimpleNamespace(current={"pct": 0.6949, "cfi": "epubcfi(/6/2)"})

    locator = SyncManager._fixed_page_locator_from_state(client, state, 0.6949)

    assert locator == LocatorResult(percentage=0.6949)


def test_abs_ebook_never_writes_fixed_page_as_cfi():
    transport = MagicMock()
    client = ABSEbookSyncClient(transport, MagicMock())
    book = Book(abs_id="abs-book", ebook_filename="comic.cbz")

    result = client.update_progress(
        book, UpdateProgressRequest(LocatorResult(percentage=0.5, page=TEST_PAGE))
    )

    assert result.success is False
    transport.update_ebook_progress.assert_not_called()


def test_bookorbit_never_serializes_fixed_page_as_cfi():
    client = BookOrbitClient.__new__(BookOrbitClient)
    client._make_request = MagicMock(return_value=SimpleNamespace(status_code=204))

    assert client.update_ebook_progress(
        {"id": 42, "ebookFileId": 77},
        0.5,
        LocatorResult(percentage=0.5, page=TEST_PAGE),
    ) is True

    payload = client._make_request.call_args.args[2]
    assert payload == {"percentage": 50.0}


def test_storyteller_never_serializes_fixed_page_as_cfi():
    client = StorytellerAPIClient.__new__(StorytellerAPIClient)

    payload = client._build_position_payload(
        "book-uuid", 0.5, LocatorResult(percentage=0.5, page=TEST_PAGE)
    )

    locations = payload["locator"]["locations"]
    assert locations == {"totalProgression": 0.5}
    assert "bookbridge" not in repr(payload)


def test_coerce_page_rejects_fractional_and_nonfinite_values():
    assert coerce_page("16") == 16
    assert coerce_page(16.5) is None
    assert coerce_page(float("nan")) is None
