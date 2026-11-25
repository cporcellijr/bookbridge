import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def install_stubs():
    api_clients = types.ModuleType("src.api.api_clients")
    api_clients.KoSyncClient = object
    sys.modules[api_clients.__name__] = api_clients

    models = types.ModuleType("src.db.models")
    models.Book = object
    models.State = object
    sys.modules[models.__name__] = models

    ebook_utils = types.ModuleType("src.utils.ebook_utils")
    ebook_utils.EbookParser = object
    sys.modules[ebook_utils.__name__] = ebook_utils

    config_loader = types.ModuleType("src.utils.config_loader")
    config_loader.env_truthy = lambda key: True
    sys.modules[config_loader.__name__] = config_loader

    progress_metadata = types.ModuleType("src.utils.progress_metadata")
    progress_metadata.parse_service_timestamp = lambda value: value
    progress_metadata.get_kosync_approved_rewind_at = lambda state: None
    progress_metadata.get_kosync_synced_pct = lambda state: getattr(state, "percentage", None)
    sys.modules[progress_metadata.__name__] = progress_metadata

    iface = types.ModuleType("src.sync_clients.sync_client_interface")

    class SyncClient:
        def __init__(self, ebook_parser):
            self.ebook_parser = ebook_parser

        def supports_book(self, book):
            return True

    class SyncResult:
        def __init__(self, location=None, success=False, updated_state=None, skipped=False):
            self.location = location
            self.success = success
            self.updated_state = updated_state
            self.skipped = skipped

    class UpdateProgressRequest:
        def __init__(self, locator_result):
            self.locator_result = locator_result

    class LocatorResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ServiceState:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    iface.SyncClient = SyncClient
    iface.SyncResult = SyncResult
    iface.UpdateProgressRequest = UpdateProgressRequest
    iface.LocatorResult = LocatorResult
    iface.ServiceState = ServiceState
    sys.modules[iface.__name__] = iface


class FakeParser:
    def __init__(self, path):
        self.path = Path(path)
        self.resolve_calls = []

    def resolve_book_path(self, filename):
        return self.path

    def get_sentence_level_ko_xpath(self, epub, pct):
        return "/body/DocFragment[2]/body/div/p[3]/span[1]/text().17"

    def resolve_xpath_to_index(self, epub, xpath):
        self.resolve_calls.append((epub, xpath))
        return 4242


class FakeKoSync:
    def __init__(self, success=True):
        self.success = success
        self.calls = []

    def is_configured(self):
        return True

    def update_progress(self, doc_id, pct, xpath, fill_if_absent=False, read_started_at=None):
        self.calls.append((doc_id, pct, xpath, fill_if_absent, read_started_at))
        return self.success


class KoSyncSyncClientCanonicalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._stub_names = (
            "src.api.api_clients",
            "src.db.models",
            "src.utils.ebook_utils",
            "src.utils.config_loader",
            "src.utils.progress_metadata",
            "src.sync_clients.sync_client_interface",
            "src.sync_clients.kosync_sync_client",
        )
        cls._saved_modules = {name: sys.modules.get(name) for name in cls._stub_names}
        install_stubs()
        sys.modules.pop("src.sync_clients.kosync_sync_client", None)
        cls.mod = importlib.import_module("src.sync_clients.kosync_sync_client")

    @classmethod
    def tearDownClass(cls):
        for name, previous in cls._saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b"epub")
        tmp.close()
        self.path = Path(tmp.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def _request(self, pct):
        return SimpleNamespace(locator_result=SimpleNamespace(percentage=pct),
                               allow_rewind=False, current_state=None, fill_if_absent=False)

    def _book(self):
        return SimpleNamespace(
            abs_id="book-1",
            kosync_doc_id="a" * 32,
            original_ebook_filename="book.epub",
            ebook_filename=None,
            abs_title="Book",
        )

    def _client(self, transport, parser):
        # Construction no longer has global side effects: the persistent cache is
        # consulted explicitly by kosync_server, not installed from __init__.
        return self.mod.KoSyncSyncClient(transport, parser)

    def test_bridge_write_resolves_and_prewarms_exact_safe_xpath(self):
        parser = FakeParser(self.path)
        transport = FakeKoSync()
        client = self._client(transport, parser)
        book = self._book()
        prewarm_calls = []
        old_prewarm = self.mod.prewarm_xpath_order_cache
        self.mod.prewarm_xpath_order_cache = lambda *args: prewarm_calls.append(args) or True
        try:
            result = client.update_progress(book, self._request(0.42))
        finally:
            self.mod.prewarm_xpath_order_cache = old_prewarm

        expected_xpath = "/body/DocFragment[2]/body/div/p[3].0"
        self.assertTrue(result.success)
        self.assertEqual(transport.calls, [("a" * 32, 0.42, expected_xpath, False, None)])
        self.assertEqual(parser.resolve_calls, [("book.epub", expected_xpath)])
        self.assertEqual(result.updated_state, {"pct": 0.42, "xpath": expected_xpath})
        self.assertEqual(len(prewarm_calls), 1)
        self.assertIs(prewarm_calls[0][0], book)
        self.assertIs(prewarm_calls[0][1], parser)
        self.assertEqual(prewarm_calls[0][2], expected_xpath)
        self.assertEqual(prewarm_calls[0][3], 4242)
        self.assertEqual(len(prewarm_calls[0][4]), 64)

    def test_disabled_xpath_ordering_adds_no_write_side_parse(self):
        parser = FakeParser(self.path)
        transport = FakeKoSync()
        client = self._client(transport, parser)
        old_env_truthy = self.mod.env_truthy
        self.mod.env_truthy = lambda key: False
        try:
            result = client.update_progress(self._book(), self._request(0.42))
        finally:
            self.mod.env_truthy = old_env_truthy

        self.assertTrue(result.success)
        self.assertEqual(parser.resolve_calls, [])

    def test_canonical_failure_does_not_block_existing_write(self):
        parser = FakeParser(self.path)
        parser.resolve_xpath_to_index = lambda epub, xpath: None
        transport = FakeKoSync()
        client = self._client(transport, parser)

        result = client.update_progress(self._book(), self._request(0.42))

        self.assertTrue(result.success)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.updated_state["pct"], 0.42)

    def test_clear_progress_does_not_create_canonical_metadata(self):
        parser = FakeParser(self.path)
        transport = FakeKoSync()
        client = self._client(transport, parser)

        result = client.update_progress(self._book(), self._request(0.0))

        self.assertEqual(transport.calls, [("a" * 32, 0.0, "", False, None)])
        self.assertEqual(result.updated_state, {"pct": 0.0, "xpath": ""})
        self.assertEqual(parser.resolve_calls, [])

    def test_service_state_stamps_read_before_get(self):
        events = []
        parser = FakeParser(self.path)
        transport = FakeKoSync()
        transport.get_progress_with_metadata = lambda doc_id: events.append("get") or (
            0.5, "/body/DocFragment[1]/body/p[1].0", {"timestamp": 1700000000},
        )
        client = self._client(transport, parser)
        original_time = self.mod.time.time
        self.mod.time.time = lambda: events.append("clock") or 1700000001.0
        try:
            state = client.get_service_state(self._book(), None)
        finally:
            self.mod.time.time = original_time
        self.assertLess(events.index("clock"), events.index("get"))
        self.assertEqual(state.kosync_read_started_at, 1700000001.0)

    def test_bulk_no_record_state_uses_the_bulk_lookup_snapshot(self):
        transport = FakeKoSync()
        client = self._client(transport, FakeParser(self.path))
        book = self._book()
        state = client.get_service_state(book, None, bulk_context={
            "documents_without_progress": {book.kosync_doc_id: book.abs_id},
            "read_started_at": 1700000001.0,
        })
        self.assertTrue(state.follower_only)
        self.assertEqual(state.kosync_read_started_at, 1700000001.0)
        self.assertEqual(transport.calls, [])

    def test_epub_write_forwards_cycle_read_snapshot(self):
        transport = FakeKoSync()
        client = self._client(transport, FakeParser(self.path))
        state = SimpleNamespace(
            kosync_read_started_at=1700000001.5,
            current={"pct": 0.4, "xpath": "/body/DocFragment[1]/body/p[1].0"},
        )
        request = self._request(0.42)
        request.current_state = state
        result = client.update_progress(self._book(), request)
        self.assertTrue(result.success)
        self.assertEqual(transport.calls[0][4], 1700000001.5)

    def test_stale_snapshot_rejection_is_a_successful_skip(self):
        transport = FakeKoSync(success=None)
        transport.get_progress_with_metadata = lambda _doc_id: (
            0.60, "/body/DocFragment[1]/body/p[2].0", {},
        )
        client = self._client(transport, FakeParser(self.path))
        current_state = SimpleNamespace(
            kosync_read_started_at=1700000001.5,
            previous_pct=0.4,
            current={"pct": 0.4, "xpath": "/body/DocFragment[1]/body/p[1].0"},
        )
        request = self._request(0.42)
        request.current_state = current_state
        result = client.update_progress(self._book(), request)
        self.assertTrue(result.success)
        self.assertTrue(result.skipped)
        self.assertEqual(result.updated_state["pct"], 0.60)
        self.assertEqual(result.updated_state["synced_pct"], 0.4)

    def test_stale_snapshot_skip_keeps_the_approved_rewind_cutoff(self):
        transport = FakeKoSync(success=None)
        transport.get_progress_with_metadata = lambda _doc_id: (
            0.60, "/body/DocFragment[1]/body/p[2].0", {},
        )
        client = self._client(transport, FakeParser(self.path))
        current_state = SimpleNamespace(
            kosync_read_started_at=1700000001.5,
            previous_pct=0.4,
            current={"pct": 0.4, "xpath": "/body/DocFragment[1]/body/p[1].0",
                     "kosync_approved_rewind_at": 1699999000.0},
        )
        request = self._request(0.42)
        request.current_state = current_state
        result = client.update_progress(self._book(), request)
        self.assertTrue(result.success and result.skipped)
        self.assertEqual(result.updated_state["kosync_approved_rewind_at"], 1699999000.0)

    def test_stale_snapshot_reread_failure_does_not_return_old_state(self):
        transport = FakeKoSync(success=None)
        transport.get_progress_with_metadata = lambda _doc_id: (None, None, {"_http_status": 500})
        client = self._client(transport, FakeParser(self.path))
        current_state = SimpleNamespace(
            kosync_read_started_at=1700000001.5,
            previous_pct=0.4,
            current={"pct": 0.4, "xpath": "/body/DocFragment[1]/body/p[1].0"},
        )
        request = self._request(0.42)
        request.current_state = current_state
        result = client.update_progress(self._book(), request)
        self.assertFalse(result.success)
        self.assertEqual(result.updated_state, {})

    def test_follower_stale_skip_keeps_zero_synced_baseline(self):
        transport = FakeKoSync(success=None)
        transport.get_progress_with_metadata = lambda _doc_id: (
            0.60, "/body/DocFragment[1]/body/p[2].0", {},
        )
        client = self._client(transport, FakeParser(self.path))
        request = self._request(0.11)
        request.kosync_read_started_at = 1700000001.5
        request.kosync_synced_pct = 0.0
        result = client.update_progress(self._book(), request)
        self.assertTrue(result.success and result.skipped)
        self.assertEqual(result.updated_state["pct"], 0.60)
        self.assertEqual(result.updated_state["synced_pct"], 0.0)

    def test_cbz_write_forwards_cycle_read_snapshot(self):
        transport = FakeKoSync()
        client = self._client(transport, FakeParser(self.path))
        book = self._book()
        book.original_ebook_filename = "book.cbz"
        state = SimpleNamespace(kosync_read_started_at=1700000001.5, current={"pct": 0.4})
        request = SimpleNamespace(
            locator_result=SimpleNamespace(percentage=0.42, page=42),
            allow_rewind=False, current_state=state, fill_if_absent=False,
        )
        with unittest.mock.patch.object(self.mod, "count_cbz_pages", return_value=100), \
                unittest.mock.patch.object(self.mod, "estimate_cbz_page", return_value=42):
            result = client.update_progress(book, request)
        self.assertTrue(result.success)
        self.assertEqual(transport.calls[0][4], 1700000001.5)

    def test_cbz_stale_skip_preserves_previous_page_baseline(self):
        transport = FakeKoSync(success=None)
        transport.get_progress_with_metadata = lambda _doc_id: (0.60, "60", {})
        client = self._client(transport, FakeParser(self.path))
        book = self._book()
        book.original_ebook_filename = "book.cbz"
        state = SimpleNamespace(
            kosync_read_started_at=1700000001.5, previous_pct=0.4,
            current={"pct": 0.4, "_previous_page": 42},
        )
        request = SimpleNamespace(
            locator_result=SimpleNamespace(percentage=0.42, page=44),
            allow_rewind=False, current_state=state, fill_if_absent=False,
        )
        with unittest.mock.patch.object(self.mod, "count_cbz_pages", return_value=100), \
                unittest.mock.patch.object(self.mod, "percentage_from_cbz_page", return_value=0.60):
            result = client.update_progress(book, request)
        self.assertTrue(result.skipped)
        self.assertEqual(result.updated_state["pct"], 0.60)
        self.assertEqual(result.updated_state["synced_pct"], 0.4)
        self.assertEqual(result.updated_state["page"], 42)


if __name__ == "__main__":
    unittest.main()
