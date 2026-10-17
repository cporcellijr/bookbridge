"""Persistent KoSync XPath-order cache (#389, hardened).

The cache is a pure optimization: every row is derivable by re-parsing the EPUB.
These tests pin the properties that make it safe to keep — a row is bound to one
exact EPUB version, failed resolutions are never persisted, the table is bounded,
and the KoSync GET path consults it explicitly rather than through a monkey patch.
"""

import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.db.database_service import DatabaseService
from src.utils import kosync_canonical as mod


class FakeParser:
    def __init__(self, path, mapping=None):
        self.path = Path(path)
        self.mapping = mapping or {}
        self.calls = []

    def resolve_book_path(self, filename):
        return self.path

    def resolve_xpath_to_index(self, filename, xpath):
        self.calls.append((filename, xpath))
        return self.mapping.get(xpath)


def _temp_epub(tmpdir, content=b"epub-v1"):
    path = Path(tmpdir) / "book.epub"
    path.write_bytes(content)
    return path


class CanonicalCacheTests(unittest.TestCase):
    """The module's own persistence helpers, against a real SQLite DatabaseService.

    `kosync_canonical` finds the database through `kosync_server._database_service`,
    so a stand-in module is installed for that lookup only — the database itself is
    real, and the table comes from the ORM model, which is what proves the model and
    the shipped migration agree.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.path = _temp_epub(self.tmpdir)
        self.parser = FakeParser(self.path, {"/device": 100, "/synced": 200})
        self.db = DatabaseService(str(Path(self.tmpdir) / "test.db"))
        self.document = None
        self.user_progress = None
        self.db.get_kosync_document = lambda _doc_id: self.document
        self.db.get_user_kosync_progress = lambda _doc_id, _user_id=None: self.user_progress

        self._old_api_pkg = sys.modules.get("src.api")
        api_pkg = self._old_api_pkg or types.ModuleType("src.api")
        sys.modules["src.api"] = api_pkg
        self._old_server_module = sys.modules.get("src.api.kosync_server")
        self._had_server_module = "src.api.kosync_server" in sys.modules
        server = types.ModuleType("src.api.kosync_server")
        server._database_service = self.db
        self.memory = {}
        server._xpath_index_cache_put = lambda key, d, s: self.memory.__setitem__(key, (d, s))
        server._xpath_index_cache_get = lambda key: self.memory.get(key)
        sys.modules["src.api.kosync_server"] = server
        self._old_server_attr = getattr(api_pkg, "kosync_server", None)
        api_pkg.kosync_server = server

    def tearDown(self):
        if self._had_server_module:
            sys.modules["src.api.kosync_server"] = self._old_server_module
        else:
            sys.modules.pop("src.api.kosync_server", None)
        api_pkg = sys.modules.get("src.api")
        if api_pkg is not None:
            if self._old_server_attr is None:
                try:
                    delattr(api_pkg, "kosync_server")
                except AttributeError:
                    pass
            else:
                api_pkg.kosync_server = self._old_server_attr
        if self._old_api_pkg is None:
            sys.modules.pop("src.api", None)
        else:
            sys.modules["src.api"] = self._old_api_pkg

    def _stored(self, key):
        return self.db.get_kosync_xpath_order(
            mod._pair_hash(key), mod.canonical_file_key(self.parser, "book.epub")
        )

    def test_file_key_changes_when_epub_changes(self):
        first = mod.canonical_file_key(self.parser, "book.epub")
        time.sleep(0.002)
        self.path.write_bytes(b"epub-v2-longer")
        os.utime(self.path, None)
        second = mod.canonical_file_key(self.parser, "book.epub")
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_persisted_pair_round_trips(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        file_key = mod.canonical_file_key(self.parser, "book.epub")

        mod._persist_pair(key, 100, 200, file_key)

        self.assertEqual(mod.load_persisted_pair(key, self.parser), (100, 200))

    def test_persisted_pair_is_ignored_after_epub_replacement(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        mod._persist_pair(key, 100, 200, file_key)

        time.sleep(0.002)
        self.path.write_bytes(b"changed-version-longer")
        os.utime(self.path, None)

        self.assertIsNone(mod.load_persisted_pair(key, self.parser))

    def test_failed_resolution_is_not_persisted(self):
        """A newer parser may resolve the same pair later — never store a negative."""
        key = ("a" * 32, "book.epub", "/device", "/unresolved")
        file_key = mod.canonical_file_key(self.parser, "book.epub")

        mod._persist_pair(key, None, None, file_key)

        self.assertIsNone(self._stored(key))

    def test_persistent_cache_is_bounded_per_document(self):
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        doc_id = "a" * 32
        old_limit = mod._PERSISTED_MAX_PER_DOCUMENT
        mod._PERSISTED_MAX_PER_DOCUMENT = 3
        try:
            for idx in range(6):
                key = (doc_id, "book.epub", f"/device-{idx}", f"/synced-{idx}")
                mod._persist_pair(key, idx, idx + 100, file_key)
                time.sleep(0.002)
            kept = [
                idx for idx in range(6)
                if self._stored((doc_id, "book.epub", f"/device-{idx}", f"/synced-{idx}")) is not None
            ]
            self.assertEqual(kept, [3, 4, 5])
        finally:
            mod._PERSISTED_MAX_PER_DOCUMENT = old_limit

    def test_cache_failure_degrades_to_a_miss(self):
        """An unusable cache must never raise into the caller."""
        key = ("a" * 32, "book.epub", "/device", "/synced")
        file_key = mod.canonical_file_key(self.parser, "book.epub")

        def boom(*_args, **_kwargs):
            raise RuntimeError("cache table missing")

        self.db.get_kosync_xpath_order = boom
        self.db.save_kosync_xpath_order = boom

        self.assertIsNone(mod.load_persisted_pair(key, self.parser))
        mod._persist_pair(key, 100, 200, file_key)  # must not raise

    def test_bridge_write_prewarm_resolves_device_xpath_off_get_path(self):
        self.document = SimpleNamespace(filename="book.epub", user_id=7, progress="/device")
        self.user_progress = SimpleNamespace(progress="/device")
        book = SimpleNamespace(
            kosync_doc_id="a" * 32,
            ebook_filename="book.epub",
            original_ebook_filename=None,
        )
        file_key = mod.canonical_file_key(self.parser, "book.epub")
        key = ("a" * 32, "book.epub", "/device", "/synced")
        old_user_id = mod._current_user_id
        mod._current_user_id = lambda: 7
        try:
            self.assertTrue(
                mod.prewarm_xpath_order_cache(book, self.parser, "/synced", 200, file_key)
            )
            deadline = time.time() + 2
            while self._stored(key) is None and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(self._stored(key), (100, 200))
            self.assertEqual(self.memory.get(key), (100, 200))
            self.assertIn(("book.epub", "/device"), self.parser.calls)
        finally:
            mod._current_user_id = old_user_id


class GetPathWiringTests(unittest.TestCase):
    """The real `kosync_server._xpath_index_cache_get` consults the persistent cache.

    #389 originally achieved this by reassigning the module's function from
    `KoSyncSyncClient.__init__`. It is now an explicit call, so these tests exercise
    the real function. Module globals are saved and restored so the suite still
    passes in any order.
    """

    def setUp(self):
        from src.api import kosync_server

        self.server = kosync_server
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.path = _temp_epub(self.tmpdir)
        self.parser = FakeParser(self.path, {"/device": 100, "/synced": 200})
        self.db = DatabaseService(str(Path(self.tmpdir) / "test.db"))

        self._saved_db = kosync_server._database_service
        self._saved_container = kosync_server._container
        self._saved_cache = dict(kosync_server._XPATH_INDEX_CACHE)
        kosync_server._database_service = self.db
        kosync_server._container = SimpleNamespace(ebook_parser=lambda: self.parser)
        kosync_server._XPATH_INDEX_CACHE.clear()

    def tearDown(self):
        self.server._database_service = self._saved_db
        self.server._container = self._saved_container
        self.server._XPATH_INDEX_CACHE.clear()
        self.server._XPATH_INDEX_CACHE.update(self._saved_cache)

    def _persist(self, key):
        mod._persist_pair(key, 100, 200, mod.canonical_file_key(self.parser, "book.epub"))

    def test_ram_miss_falls_through_to_the_persistent_cache(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        self._persist(key)

        self.assertEqual(self.server._xpath_index_cache_get(key), (100, 200))

    def test_persistent_hit_repopulates_the_in_memory_cache(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        self._persist(key)

        self.server._xpath_index_cache_get(key)

        self.assertIn(key, self.server._XPATH_INDEX_CACHE)
        # A second read is served from RAM without touching the database at all.
        self.db.get_kosync_xpath_order = lambda *_a, **_k: self.fail("second read hit the DB")
        self.assertEqual(self.server._xpath_index_cache_get(key), (100, 200))

    def test_cached_failed_resolution_is_returned_from_ram(self):
        """#386 caches an unresolvable pair as (None, None); that must not fall through."""
        key = ("a" * 32, "book.epub", "/device", "/unresolvable")
        self.server._xpath_index_cache_put(key, None, None)
        self.db.get_kosync_xpath_order = lambda *_a, **_k: self.fail("consulted DB for a RAM-cached failure")

        self.assertEqual(self.server._xpath_index_cache_get(key), (None, None))

    def test_get_path_resolutions_are_never_persisted(self):
        """The GET path has no before/after file-version proof, so it stays RAM-only."""
        key = ("a" * 32, "book.epub", "/device", "/synced")

        self.server._xpath_index_cache_put(key, 100, 200)

        stored = self.db.get_kosync_xpath_order(
            mod._pair_hash(key), mod.canonical_file_key(self.parser, "book.epub")
        )
        self.assertIsNone(stored)

    def test_miss_with_no_persisted_pair_returns_none(self):
        key = ("a" * 32, "book.epub", "/device", "/never-seen")

        self.assertIsNone(self.server._xpath_index_cache_get(key))

    def test_stubbed_server_preserves_import_identity(self) -> None:
        """Running a stubbed CanonicalCacheTests case must not leak the stub module."""
        import importlib

        original = importlib.import_module("src.api.kosync_server")
        case = CanonicalCacheTests("test_persisted_pair_round_trips")
        result = unittest.TestResult()
        case.run(result)
        self.assertTrue(
            result.wasSuccessful(),
            f"inner test failed: {result.failures + result.errors}"
        )
        fresh = importlib.import_module("src.api.kosync_server")
        self.assertIs(fresh, original)
        self.assertIs(sys.modules["src.api"].kosync_server, original)

    def test_unexpected_key_shape_is_a_miss(self):
        self.assertIsNone(self.server._xpath_index_cache_get(("too", "short")))

    def test_missing_container_is_a_miss_not_an_error(self):
        key = ("a" * 32, "book.epub", "/device", "/synced")
        self._persist(key)
        self.server._container = None

        self.assertIsNone(self.server._xpath_index_cache_get(key))


if __name__ == "__main__":
    unittest.main()
