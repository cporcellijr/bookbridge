import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.sync_manager import SyncManager
from src.utils.user_context import get_current_user_id, set_current_user_id, reset_current_user_id


class _FakeClient:
    def is_configured(self):
        return True


class _FakeBook:
    def __init__(self, abs_id, sync_mode="audiobook", audio_source="ABS"):
        self.abs_id = abs_id
        self.abs_title = abs_id
        self.sync_mode = sync_mode
        self.audio_source = audio_source
        self.status = "pending"


class _FakeABSApi:
    def __init__(self, accessible):
        self.accessible = set(accessible)
        self.calls = []

    def is_configured(self):
        return True

    def get_item_details(self, abs_id):
        self.calls.append(abs_id)
        return {"id": abs_id} if abs_id in self.accessible else None


class _FakeABSSyncClient(_FakeClient):
    def __init__(self, accessible=()):
        self.abs_client = _FakeABSApi(accessible)


class _FakeStateDB:
    def __init__(self, state_abs_ids=(), linked=None):
        self.state_abs_ids = set(state_abs_ids)
        self.linked = linked or {}  # user_id -> set(abs_id)

    def get_states_for_book(self, abs_id):
        return [object()] if abs_id in self.state_abs_ids else []

    def is_user_linked(self, user_id, abs_id):
        return abs_id in self.linked.get(user_id, set())


class _FailingLinkDB(_FakeStateDB):
    def is_user_linked(self, user_id, abs_id):
        raise RuntimeError("db unavailable")


class _FakeBundle:
    def __init__(
        self,
        sync_clients,
        user_id=None,
        library_service=None,
        abs_client=None,
        booklore_client=None,
        bookorbit_client=None,
        storyteller_client=None,
    ):
        self.user_id = user_id
        self.sync_clients = sync_clients
        self.library_service = library_service
        self.abs_client = abs_client
        self.booklore_client = booklore_client
        self.bookorbit_client = bookorbit_client
        self.storyteller_client = storyteller_client
        self.credentials = {}


class _FakeRegistry:
    def __init__(self, mapping):
        self._mapping = mapping  # user_id -> bundle

    def get_clients(self, user_id):
        return self._mapping[user_id]


def _make_manager(global_clients, registry=None):
    mgr = SyncManager.__new__(SyncManager)
    mgr._global_sync_clients = dict(global_clients)
    mgr.user_client_registry = registry
    mgr._sync_lock = threading.Lock()
    mgr._post_cycle_callbacks = []
    mgr._dispatch_pending_syncs = lambda: None
    # Probe what the cycle "sees" for clients + ambient user.
    mgr._probe = {}

    def _internal(target_abs_id=None):
        mgr._probe['clients'] = mgr.sync_clients
        mgr._probe['user_id'] = get_current_user_id()
        mgr._probe['library_service'] = mgr.active_library_service
        mgr._probe['abs_client'] = mgr.active_abs_client
        mgr._probe['booklore_client'] = mgr.active_booklore_client
        mgr._probe['bookorbit_client'] = mgr.active_bookorbit_client
        mgr._probe['storyteller_client'] = mgr.active_storyteller_client
        mgr._probe['booklore_audio_adapter'] = mgr._get_audio_source_adapter(_FakeBook("bl", audio_source="BookLore"))

    mgr._sync_cycle_internal = _internal
    return mgr


class TestSyncCycleUserScoping(unittest.TestCase):
    def test_default_cycle_uses_global_clients_and_no_user(self):
        g = {"ABS": _FakeClient()}
        mgr = _make_manager(g, registry=_FakeRegistry({}))
        mgr.sync_cycle()
        self.assertIs(mgr._probe['clients'], mgr._global_sync_clients)
        self.assertIsNone(mgr._probe['user_id'])

    def test_user_cycle_uses_registry_clients_and_sets_context(self):
        library_service = object()
        abs_client = object()
        booklore_client = object()
        bookorbit_client = object()
        storyteller_client = object()
        per_user = {"KoSync": _FakeClient(), "Storyteller": _FakeClient()}
        registry = _FakeRegistry({
            7: _FakeBundle(
                per_user,
                library_service=library_service,
                abs_client=abs_client,
                booklore_client=booklore_client,
                bookorbit_client=bookorbit_client,
                storyteller_client=storyteller_client,
            )
        })
        mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
        mgr.library_service = object()
        mgr.data_dir = Path("/tmp")
        mgr.sync_cycle(user_id=7)
        # During the cycle the active clients were the user's bundle...
        self.assertEqual(set(mgr._probe['clients'].keys()), {"KoSync", "Storyteller"})
        self.assertEqual(mgr._probe['user_id'], 7)
        self.assertIs(mgr._probe['library_service'], library_service)
        self.assertIs(mgr._probe['abs_client'], abs_client)
        self.assertIs(mgr._probe['booklore_client'], booklore_client)
        self.assertIs(mgr._probe['bookorbit_client'], bookorbit_client)
        self.assertIs(mgr._probe['storyteller_client'], storyteller_client)
        self.assertIs(mgr._probe['booklore_audio_adapter'].booklore_client, booklore_client)

    def test_user_cycle_missing_provider_clients_does_not_fallback_to_global(self):
        registry = _FakeRegistry({8: _FakeBundle({"KoSync": _FakeClient()})})
        mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
        mgr.abs_client = object()
        mgr.booklore_client = object()
        mgr.bookorbit_client = object()
        mgr.storyteller_client = object()
        mgr.library_service = object()
        mgr.audio_source_adapters = {"BookLore": object()}

        mgr.sync_cycle(user_id=8)

        self.assertIsNone(mgr._probe['abs_client'])
        self.assertIsNone(mgr._probe['booklore_client'])
        self.assertIsNone(mgr._probe['bookorbit_client'])
        self.assertIsNone(mgr._probe['storyteller_client'])
        self.assertIsNone(mgr._probe['library_service'])
        self.assertIsNone(mgr._probe['booklore_audio_adapter'])

    def test_context_is_reset_after_cycle(self):
        per_user = {"KoSync": _FakeClient()}
        registry = _FakeRegistry({3: _FakeBundle(per_user)})
        mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
        mgr.sync_cycle(user_id=3)
        # After the cycle: ambient user cleared, clients back to global.
        self.assertIsNone(get_current_user_id())
        self.assertIs(mgr.sync_clients, mgr._global_sync_clients)
        self.assertIs(mgr.active_library_service, getattr(mgr, "library_service", None))

    def test_user_cycle_without_registry_falls_back_to_global(self):
        g = {"ABS": _FakeClient()}
        mgr = _make_manager(g, registry=None)
        mgr.sync_cycle(user_id=9)
        self.assertIs(mgr._probe['clients'], mgr._global_sync_clients)
        # no registry => no per-user context set
        self.assertIsNone(mgr._probe['user_id'])

    def test_user_cycle_registry_failure_does_not_fallback_to_global(self):
        mgr = _make_manager({"ABS": _FakeClient()}, registry=_FakeRegistry({}))
        mgr._sync_cycle_internal = unittest.mock.Mock()

        mgr.sync_cycle(user_id=99)

        mgr._sync_cycle_internal.assert_not_called()

    def test_only_configured_user_clients_are_used(self):
        class _Unconfigured:
            def is_configured(self):
                return False
        per_user = {"KoSync": _FakeClient(), "CWA": _Unconfigured()}
        registry = _FakeRegistry({1: _FakeBundle(per_user)})
        mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
        mgr.sync_cycle(user_id=1)
        self.assertEqual(set(mgr._probe['clients'].keys()), {"KoSync"})

    def test_background_job_receives_user_library_service(self):
        class _JobDB:
            def __init__(self):
                self.book = _FakeBook("book-1")

            def get_books_by_status(self, status):
                return [self.book] if status == "pending" else []

            def save_book(self, book):
                self.book = book

            def save_job(self, job):
                self.job = job

        user_library_service = object()
        global_library_service = object()
        registry = _FakeRegistry({2: _FakeBundle({"ABS": _FakeClient()}, library_service=user_library_service)})
        mgr = SyncManager.__new__(SyncManager)
        mgr._global_sync_clients = {}
        mgr.user_client_registry = registry
        mgr.library_service = global_library_service
        mgr.database_service = _JobDB()
        mgr._sync_lock = threading.Lock()
        mgr._post_cycle_callbacks = []
        mgr._dispatch_pending_syncs = lambda: None
        mgr._job_thread = None
        mgr._sync_cycle_internal = lambda target_abs_id=None: mgr.check_pending_jobs()

        from unittest.mock import patch
        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.sync_cycle(user_id=2)

        thread_cls.assert_called_once()
        args = thread_cls.call_args.kwargs["args"]
        self.assertEqual(args[:3], (mgr.database_service.book, 1, 1))
        self.assertIs(args[3], user_library_service)
        self.assertIsNot(args[3], global_library_service)
        self.assertIs(args[4], registry.get_clients(2))

    def test_global_pending_job_uses_claimant_library_service(self):
        class _JobDB:
            def __init__(self):
                self.book = _FakeBook("book-claim")

            def get_books_by_status(self, status):
                return [self.book] if status == "pending" else []

            def get_book_user_ids(self, abs_id):
                return [2] if abs_id == "book-claim" else []

            def save_book(self, book):
                self.book = book

            def save_job(self, job):
                self.job = job

        user_library_service = object()
        global_library_service = object()
        registry = _FakeRegistry({
            2: _FakeBundle(
                {"ABS": _FakeClient()},
                user_id=2,
                library_service=user_library_service,
            )
        })
        mgr = SyncManager.__new__(SyncManager)
        mgr._global_sync_clients = {}
        mgr.user_client_registry = registry
        mgr.library_service = global_library_service
        mgr.database_service = _JobDB()
        mgr._sync_lock = threading.Lock()
        mgr._post_cycle_callbacks = []
        mgr._dispatch_pending_syncs = lambda: None
        mgr._job_thread = None
        mgr._sync_cycle_internal = lambda target_abs_id=None: mgr.check_pending_jobs()

        from unittest.mock import patch
        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr.sync_cycle()

        thread_cls.assert_called_once()
        args = thread_cls.call_args.kwargs["args"]
        self.assertEqual(args[:3], (mgr.database_service.book, 1, 1))
        self.assertIs(args[3], user_library_service)
        self.assertIsNot(args[3], global_library_service)
        self.assertIs(args[4], registry.get_clients(2))

    def test_grimmory_epub_fallback_uses_active_booklore_client(self):
        class _BookLoreDownloadClient:
            def __init__(self, content=None, fail=False):
                self.content = content
                self.fail = fail
                self.find_calls = []
                self.download_calls = []

            def is_configured(self):
                return True

            def find_book_by_filename(self, filename, *args, **kwargs):
                self.find_calls.append(filename)
                if self.fail:
                    raise AssertionError("global BookLore client should not be used")
                return {"id": 77}

            def download_book(self, book_id):
                self.download_calls.append(book_id)
                return self.content

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            user_bl = _BookLoreDownloadClient(content=b"user epub")
            global_bl = _BookLoreDownloadClient(fail=True)
            registry = _FakeRegistry({
                4: _FakeBundle({"ABS": _FakeClient()}, booklore_client=user_bl)
            })
            mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
            mgr.booklore_client = global_bl
            mgr.books_dir = root / "books"
            mgr.books_dir.mkdir()
            mgr.epub_cache_dir = root / "cache"
            mgr._sync_cycle_local_epub_cache = {}

            def _internal(target_abs_id=None):
                mgr._probe["epub_path"] = mgr._get_local_epub("User Book.epub")

            mgr._sync_cycle_internal = _internal
            mgr.sync_cycle(user_id=4)

            self.assertEqual(user_bl.find_calls, ["User Book.epub"])
            self.assertEqual(user_bl.download_calls, [77])
            self.assertEqual(Path(mgr._probe["epub_path"]).read_bytes(), b"user epub")

    def test_grimmory_reading_session_uses_active_booklore_client(self):
        class _BookLoreSessionClient:
            def __init__(self):
                self.session_calls = []

            def create_reading_session(self, **kwargs):
                self.session_calls.append(kwargs)

        user_bl = _BookLoreSessionClient()
        global_bl = _BookLoreSessionClient()
        registry = _FakeRegistry({
            5: _FakeBundle({"ABS": _FakeClient()}, booklore_client=user_bl)
        })
        mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
        mgr.booklore_client = global_bl
        mgr._compute_session_duration = lambda *args, **kwargs: 120
        book = SimpleNamespace(
            abs_id="booklore:123",
            audio_source="BookLore",
            audio_source_id="123",
            ebook_filename="book.epub",
        )
        leader_state = SimpleNamespace(current={"pct": 0.5}, previous_pct=0.4)

        def _internal(target_abs_id=None):
            mgr._record_grimmory_reading_session(book, "BookLoreAudio", leader_state, {}, 1000.0)

        mgr._sync_cycle_internal = _internal
        mgr.sync_cycle(user_id=5)

        self.assertEqual(len(user_bl.session_calls), 1)
        self.assertEqual(user_bl.session_calls[0]["book_id"], 123)
        self.assertEqual(global_bl.session_calls, [])

    def test_bookorbit_reading_session_uses_active_bookorbit_client(self):
        class _BookOrbitSessionClient:
            def __init__(self):
                self.session_calls = []

            def create_reading_session(self, **kwargs):
                self.session_calls.append(kwargs)

        user_bo = _BookOrbitSessionClient()
        global_bo = _BookOrbitSessionClient()
        registry = _FakeRegistry({
            6: _FakeBundle({"ABS": _FakeClient()}, bookorbit_client=user_bo)
        })
        mgr = _make_manager({"ABS": _FakeClient()}, registry=registry)
        mgr.bookorbit_client = global_bo
        mgr._compute_session_duration = lambda *args, **kwargs: 90
        book = SimpleNamespace(
            abs_id="bookorbit-book",
            audio_source="ABS",
            ebook_source="BookOrbit",
            ebook_source_id="42",
            ebook_filename="book.epub",
        )
        leader_state = SimpleNamespace(current={"pct": 0.6, "cfi": "epubcfi(/6/2)"}, previous_pct=0.5)

        def _internal(target_abs_id=None):
            mgr._record_bookorbit_reading_session(book, "BookOrbit", leader_state, {}, 1000.0)

        mgr._sync_cycle_internal = _internal
        mgr.sync_cycle(user_id=6)

        self.assertEqual(len(user_bo.session_calls), 1)
        self.assertEqual(user_bo.session_calls[0]["book_id"], 42)
        self.assertEqual(user_bo.session_calls[0]["end_location"], "epubcfi(/6/2)")
        self.assertEqual(global_bo.session_calls, [])

    def test_user_scoped_suggestion_uses_user_provider_clients(self):
        class _SuggestionDB:
            def __init__(self):
                self.saved = []

            def suggestion_exists(self, abs_id):
                return False

            def get_all_books(self):
                return []

            def save_pending_suggestion(self, suggestion):
                self.saved.append(suggestion)

        class _AbsSuggestionClient:
            def __init__(self, label):
                self.label = label
                self.calls = []

            def get_progress(self, abs_id):
                self.calls.append(("progress", abs_id))
                return {"progress": 0.2}

            def get_item_details(self, abs_id):
                self.calls.append(("details", abs_id))
                return {
                    "media": {
                        "metadata": {
                            "title": "User Book",
                            "authorName": "Author",
                        }
                    }
                }

            def get_ebook_files(self, abs_id):
                self.calls.append(("ebook_files", abs_id))
                return []

            def search_ebooks(self, term):
                self.calls.append(("search_ebooks", term))
                return []

        class _BookLoreSuggestionClient:
            def __init__(self, label):
                self.label = label
                self.calls = []

            def is_configured(self):
                return True

            def search_books(self, term):
                self.calls.append(("search_books", term))
                return [{"id": 9, "title": "User Book", "fileName": "User Book.epub"}]

        class _CwaSuggestionClient:
            def __init__(self, label):
                self.label = label
                self.calls = []

            def is_configured(self):
                return True

            def search_ebooks(self, term):
                self.calls.append(("search_ebooks", term))
                return []

        user_abs = _AbsSuggestionClient("user")
        global_abs = _AbsSuggestionClient("global")
        user_bl = _BookLoreSuggestionClient("user")
        global_bl = _BookLoreSuggestionClient("global")
        user_cwa = _CwaSuggestionClient("user")
        global_cwa = _CwaSuggestionClient("global")
        user_library = SimpleNamespace(cwa_client=user_cwa)
        global_library = SimpleNamespace(cwa_client=global_cwa)

        registry = _FakeRegistry({
            2: _FakeBundle(
                {"ABS": _FakeClient()},
                abs_client=user_abs,
                booklore_client=user_bl,
                library_service=user_library,
            )
        })
        mgr = SyncManager.__new__(SyncManager)
        mgr.user_client_registry = registry
        mgr.database_service = _SuggestionDB()
        mgr.abs_client = global_abs
        mgr.booklore_client = global_bl
        mgr.library_service = global_library
        mgr.books_dir = None
        mgr._suggestion_lock = threading.Lock()
        mgr._suggestion_in_flight = set()

        mgr.queue_suggestion("book-1", user_id=2)

        self.assertEqual([call[0] for call in user_abs.calls], ["progress", "details", "ebook_files", "search_ebooks"])
        self.assertEqual(user_bl.calls, [("search_books", "User Book")])
        self.assertEqual(user_cwa.calls, [("search_ebooks", "User Book Author")])
        self.assertEqual(global_abs.calls, [])
        self.assertEqual(global_bl.calls, [])
        self.assertEqual(global_cwa.calls, [])
        self.assertEqual(len(mgr.database_service.saved), 1)

    def test_user_filter_skips_abs_books_not_accessible_to_user_token(self):
        mgr = SyncManager.__new__(SyncManager)
        mgr.sync_clients = {"ABS": _FakeABSSyncClient(accessible={"allowed"})}
        # All three are this user's claimed books (linked); the ABS token still
        # gates the audio items by accessibility.
        mgr.database_service = _FakeStateDB(
            state_abs_ids={"ebook"}, linked={2: {"allowed", "forbidden", "ebook"}}
        )
        books = [
            _FakeBook("allowed"),
            _FakeBook("forbidden"),
            _FakeBook("ebook", sync_mode="ebook_only"),
        ]

        token = set_current_user_id(2)
        try:
            visible = mgr._filter_books_for_current_user(books, bulk_states_per_client={})
        finally:
            reset_current_user_id(token)

        self.assertEqual([b.abs_id for b in visible], ["allowed", "ebook"])
        self.assertEqual(
            mgr.sync_clients["ABS"].abs_client.calls,
            ["allowed", "forbidden"],
        )

    def test_user_filter_allows_linked_ebook_only_book_without_state_rows(self):
        # An ebook-only book the user has claimed (user_books link) must sync even
        # before any state row exists, so the first KoSync read can bootstrap it.
        # Regression: previously skipped forever (no state -> skipped -> no state).
        mgr = SyncManager.__new__(SyncManager)
        mgr.sync_clients = {"ABS": _FakeABSSyncClient(accessible=set())}
        mgr.database_service = _FakeStateDB(state_abs_ids=set(), linked={1: {"ebook-linked"}})
        books = [
            _FakeBook("ebook-linked", sync_mode="ebook_only"),
            _FakeBook("ebook-unlinked", sync_mode="ebook_only"),
        ]

        token = set_current_user_id(1)
        try:
            visible = mgr._filter_books_for_current_user(books, bulk_states_per_client={})
        finally:
            reset_current_user_id(token)

        self.assertEqual([b.abs_id for b in visible], ["ebook-linked"])

    def test_user_filter_skips_book_with_state_but_no_link(self):
        # Cross-user leak guard: a state mis-attributed to this user (e.g. a device
        # that authenticated as the admin) on a book they do NOT own (no link) must
        # not sync — otherwise it would be pushed to this user's ABS/StoryGraph.
        mgr = SyncManager.__new__(SyncManager)
        mgr.sync_clients = {"ABS": _FakeABSSyncClient(accessible={"dcc"})}
        mgr.database_service = _FakeStateDB(state_abs_ids={"dcc"}, linked={})
        books = [_FakeBook("dcc")]

        token = set_current_user_id(1)
        try:
            visible = mgr._filter_books_for_current_user(books, bulk_states_per_client={})
        finally:
            reset_current_user_id(token)

        self.assertEqual(visible, [])
        # Not linked -> skipped before the ABS access check even runs.
        self.assertEqual(mgr.sync_clients["ABS"].abs_client.calls, [])

    def test_user_filter_fails_closed_when_link_check_errors(self):
        mgr = SyncManager.__new__(SyncManager)
        mgr.sync_clients = {"ABS": _FakeABSSyncClient(accessible={"book-1"})}
        mgr.database_service = _FailingLinkDB(linked={1: {"book-1"}})
        books = [_FakeBook("book-1")]

        token = set_current_user_id(1)
        try:
            visible = mgr._filter_books_for_current_user(books, bulk_states_per_client={})
        finally:
            reset_current_user_id(token)

        self.assertEqual(visible, [])
        self.assertEqual(mgr.sync_clients["ABS"].abs_client.calls, [])

    def test_user_filter_allows_abs_books_seen_in_bulk_without_detail_call(self):
        mgr = SyncManager.__new__(SyncManager)
        mgr.sync_clients = {"ABS": _FakeABSSyncClient(accessible=set())}
        mgr.database_service = _FakeStateDB(linked={2: {"bulk-visible"}})
        books = [_FakeBook("bulk-visible")]

        token = set_current_user_id(2)
        try:
            visible = mgr._filter_books_for_current_user(
                books,
                bulk_states_per_client={"ABS": {"bulk-visible": {"currentTime": 10}}},
            )
        finally:
            reset_current_user_id(token)

        self.assertEqual([b.abs_id for b in visible], ["bulk-visible"])
        self.assertEqual(mgr.sync_clients["ABS"].abs_client.calls, [])


class _FakeUser:
    def __init__(self, uid, active=1):
        self.id = uid
        self.active = active


class _FakeDB:
    def __init__(self, users):
        self._users = users

    def list_users(self):
        return self._users


class _Unconfigured:
    def is_configured(self):
        return False


class TestRunSyncForAllUsers(unittest.TestCase):
    def _mgr(self, users, bundles, registry=True):
        mgr = SyncManager.__new__(SyncManager)
        mgr.database_service = _FakeDB(users)
        mgr.user_client_registry = _FakeRegistry(bundles) if registry else None
        mgr._calls = []
        mgr.sync_cycle = lambda target_abs_id=None, user_id=None: mgr._calls.append((target_abs_id, user_id))
        return mgr

    def test_loops_eligible_users(self):
        bundles = {
            1: _FakeBundle({"ABS": _FakeClient()}),
            2: _FakeBundle({"KoSync": _FakeClient()}),
        }
        mgr = self._mgr([_FakeUser(1), _FakeUser(2)], bundles)
        mgr.run_sync_for_all_users(target_abs_id="bookX")
        self.assertEqual(set(mgr._calls), {("bookX", 1), ("bookX", 2)})

    def test_skips_users_with_no_configured_clients(self):
        bundles = {
            1: _FakeBundle({"ABS": _FakeClient()}),
            2: _FakeBundle({"CWA": _Unconfigured()}),
        }
        mgr = self._mgr([_FakeUser(1), _FakeUser(2)], bundles)
        mgr.run_sync_for_all_users()
        self.assertEqual(mgr._calls, [(None, 1)])

    def test_skips_inactive_users(self):
        bundles = {1: _FakeBundle({"ABS": _FakeClient()}), 2: _FakeBundle({"ABS": _FakeClient()})}
        mgr = self._mgr([_FakeUser(1), _FakeUser(2, active=0)], bundles)
        mgr.run_sync_for_all_users()
        self.assertEqual(mgr._calls, [(None, 1)])

    def test_falls_back_to_single_cycle_without_registry(self):
        mgr = self._mgr([_FakeUser(1)], {}, registry=False)
        mgr.run_sync_for_all_users(target_abs_id="b")
        self.assertEqual(mgr._calls, [("b", None)])  # default cycle, no user

    def test_falls_back_when_no_eligible_users(self):
        mgr = self._mgr([], {})
        mgr.run_sync_for_all_users()
        self.assertEqual(mgr._calls, [(None, None)])


class _BusyLock:
    def acquire(self, *args, **kwargs):
        return False


class TestPendingSyncUserScope(unittest.TestCase):
    def test_lock_timeout_queues_and_replays_with_user_id(self):
        registry = _FakeRegistry({2: _FakeBundle({"KoSync": _FakeClient()})})
        mgr = SyncManager.__new__(SyncManager)
        mgr._global_sync_clients = {}
        mgr.user_client_registry = registry
        mgr._sync_lock = _BusyLock()
        mgr._pending_sync_lock = threading.Lock()
        mgr._pending_sync_books = set()
        mgr._post_cycle_callbacks = []
        mgr._sync_cycle_internal = lambda target_abs_id=None: None

        mgr.sync_cycle(target_abs_id="book-1", user_id=2)
        self.assertEqual(mgr._pending_sync_books, {(2, "book-1")})

        from unittest.mock import patch
        with patch("src.sync_manager.threading.Thread") as thread_cls:
            mgr._dispatch_pending_syncs()

        self.assertEqual(mgr._pending_sync_books, set())
        thread_cls.assert_called_once()
        target = thread_cls.call_args.kwargs["target"]
        self.assertIs(target.__self__, mgr)
        self.assertIs(target.__func__, SyncManager.sync_cycle)
        self.assertEqual(
            thread_cls.call_args.kwargs["kwargs"],
            {'target_abs_id': 'book-1', 'user_id': 2},
        )


if __name__ == "__main__":
    unittest.main()
