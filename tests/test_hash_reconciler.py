"""Tests for the standalone KoSync hash reconciler.

Covers KOReaderDeviceSyncService.reconcile_hashes plus the daemon's setting reads:
- drifted hashes are counted as linked, unchanged ones are not
- a failing book does not abort the pass
- an unresolvable book counts as skipped
- the enable toggle honors both 'true' and 'on'
- the interval parses, falls back on garbage, and is floored at 5 minutes
- the daemon does no work while disabled
"""

import itertools
import os
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services import hash_reconciler

_FLOOR = hash_reconciler._MIN_SIGNAL_INTERVAL_SECONDS
from src.services.koreader_device_sync_service import KOReaderDeviceSyncService


def _reset_user_services():
    """No per-user service cache exists any more (services are rebuilt each pass).

    Kept as a no-op so the setUp/addCleanup call sites keep documenting that this
    module must not leak state between tests, and so re-introducing a cache has an
    obvious place to be cleared.
    """
    return None


class _Sentinel(Exception):
    """Breaks the daemon loop after one iteration."""


def _book(abs_id, title):
    return SimpleNamespace(
        abs_id=abs_id,
        abs_title=title,
        ebook_source="bookorbit",
        ebook_source_id="1",
        sync_mode="ebook_only",
        original_ebook_filename=f"{abs_id}.epub",
        ebook_filename=f"{abs_id}.epub",
        kosync_doc_id=None,
        status="active",
    )


class TestReconcileHashes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = KOReaderDeviceSyncService(
            database_service=MagicMock(),
            ebook_parser=MagicMock(),
            abs_client=MagicMock(),
            booklore_client=MagicMock(),
            cwa_client=MagicMock(),
            kavita_client=MagicMock(),
            epub_cache_dir=Path(self.tmp.name),
            bookorbit_client=MagicMock(),
        )
        self.books = [_book("a", "Book A"), _book("b", "Book B")]
        self.service._get_active_books = lambda: list(self.books)

    def test_counts_only_newly_linked_hashes(self):
        results = {
            "a": {"path": Path("a"), "content_hash": "hash-a"},
            "b": {"path": Path("b"), "content_hash": "hash-b"},
        }
        self.service._resolve_download_artifact = lambda book, link_hashes=True, allow_revalidation=False: results[book.abs_id]
        self.service.database_service.ensure_linked_kosync_document.side_effect = [True, False]

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["linked"], 1)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_failing_book_does_not_abort_the_pass(self):
        seen = []

        def resolve(book, link_hashes=True, allow_revalidation=False):
            seen.append(book.abs_id)
            if book.abs_id == "a":
                raise RuntimeError("boom")
            return {"path": Path("b"), "content_hash": "hash-b"}

        self.service._resolve_download_artifact = resolve
        self.service.database_service.ensure_linked_kosync_document.return_value = True

        summary = self.service.reconcile_hashes()

        self.assertEqual(seen, ["a", "b"], "the second book must still be processed")
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["linked"], 1)
        self.assertEqual(summary["checked"], 2)

    def test_unresolvable_book_counts_as_skipped(self):
        self.service._resolve_download_artifact = lambda book, link_hashes=True, allow_revalidation=False: None

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["skipped"], 2)
        self.assertEqual(summary["linked"], 0)
        self.assertEqual(summary["errors"], 0)

    def test_two_books_sharing_one_file_do_not_steal_the_hash(self):
        """A catalogue mis-mapping must not make passes flip the link forever.

        Observed live: 'Northern Reach' and 'Salt Marsh' both pointed at the same EPUB,
        so each pass rebound the same hash to whichever book came last.
        """
        same = {"path": Path("shared"), "content_hash": "shared-hash"}
        self.service._resolve_download_artifact = lambda book, link_hashes=True, allow_revalidation=False: dict(same)
        self.service.database_service.ensure_linked_kosync_document.return_value = True

        summary = self.service.reconcile_hashes()

        self.assertEqual(summary["conflicts"], 1)
        self.assertEqual(summary["linked"], 1, "only the first claimant links")
        self.assertEqual(
            self.service.database_service.ensure_linked_kosync_document.call_count, 1,
            "the second book must not rebind the shared hash",
        )

    def test_resolution_during_reconcile_does_not_link_internally(self):
        """reconcile must resolve with link_hashes=False so it can veto a conflict."""
        captured = {}

        def resolve(book, link_hashes=True, allow_revalidation=False):
            captured[book.abs_id] = link_hashes
            return {"path": Path("p"), "content_hash": f"hash-{book.abs_id}"}

        self.service._resolve_download_artifact = resolve
        self.service.database_service.ensure_linked_kosync_document.return_value = False

        self.service.reconcile_hashes()

        self.assertEqual(captured, {"a": False, "b": False})

    def test_link_sibling_hash_reports_whether_anything_changed(self):
        self.service.database_service.ensure_linked_kosync_document.return_value = True
        self.assertTrue(self.service._link_sibling_hash("abs-1", "hash-1"))

        self.service.database_service.ensure_linked_kosync_document.return_value = False
        self.assertFalse(self.service._link_sibling_hash("abs-1", "hash-1"))

        self.service.database_service.ensure_linked_kosync_document.side_effect = RuntimeError("db down")
        self.assertFalse(self.service._link_sibling_hash("abs-1", "hash-1"))


class TestReconcilerSettings(unittest.TestCase):
    KEYS = ("KOSYNC_HASH_RECONCILE_ENABLED", "KOSYNC_HASH_RECONCILE_MINUTES")

    def setUp(self):
        self.original = {k: os.environ.get(k) for k in self.KEYS}

    def tearDown(self):
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_enabled_accepts_true_and_on(self):
        for spelling in ("true", "on", "True", "1"):
            os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = spelling
            self.assertTrue(hash_reconciler._reconcile_enabled(), spelling)

    def test_disabled_when_false(self):
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "false"
        self.assertFalse(hash_reconciler._reconcile_enabled())

    def test_enabled_by_default(self):
        os.environ.pop("KOSYNC_HASH_RECONCILE_ENABLED", None)
        self.assertTrue(hash_reconciler._reconcile_enabled())

    def test_interval_default_and_parsing(self):
        os.environ.pop("KOSYNC_HASH_RECONCILE_MINUTES", None)
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 360 * 60)

        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "30"
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 30 * 60)

    def test_interval_falls_back_on_garbage(self):
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "not-a-number"
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 360 * 60)

    def test_interval_floored_at_five_minutes(self):
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "1"
        self.assertEqual(hash_reconciler._reconcile_interval_seconds(), 5 * 60)

    def test_daemon_skips_work_while_disabled(self):
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "false"
        service = MagicMock()

        with patch.object(hash_reconciler._wake_event, "wait", side_effect=_Sentinel()):
            with self.assertRaises(_Sentinel):
                hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        service.reconcile_hashes.assert_not_called()

    def test_daemon_reconciles_while_enabled(self):
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        service = MagicMock()

        with patch.object(hash_reconciler._wake_event, "wait", side_effect=_Sentinel()):
            with self.assertRaises(_Sentinel):
                hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        service.reconcile_hashes.assert_called_once()

    def test_pass_defers_while_a_reader_is_syncing(self):
        """A full pass mid-sync makes KOReader crawl, so it must yield."""
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        service = MagicMock()

        with patch.object(hash_reconciler, "_device_sync_busy", return_value=True):
            with patch.object(hash_reconciler._wake_event, "wait", side_effect=[None, _Sentinel()]):
                with self.assertRaises(_Sentinel):
                    hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        service.reconcile_hashes.assert_not_called()

    def test_deferral_is_bounded(self):
        """A device that polls forever must not starve reconciliation."""
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        service = MagicMock()
        service.reconcile_hashes.return_value = {}

        waits = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            if len(waits) > hash_reconciler._MAX_DEFERRALS + 1:
                raise _Sentinel()
            return False

        with patch.object(hash_reconciler, "_device_sync_busy", return_value=True):
            with patch.object(hash_reconciler, "_reconcile_all_users") as run:
                with patch.object(hash_reconciler._wake_event, "wait", side_effect=fake_wait):
                    with self.assertRaises(_Sentinel):
                        hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        self.assertGreaterEqual(run.call_count, 1, "must run once the deferral cap is hit")

    def test_busy_check_reads_device_sync_activity(self):
        import src.api.kosync_server as ks
        ks.note_device_sync_activity()
        self.assertTrue(hash_reconciler._device_sync_busy())
        ks._last_device_sync_activity = 0.0
        self.assertFalse(hash_reconciler._device_sync_busy())

    def test_signal_wakes_the_daemon_early_once_past_the_floor(self):
        """An unresolved hash must not wait a whole interval for the next pass."""
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "360"
        service = MagicMock()
        hash_reconciler._wake_event.clear()

        hash_reconciler.signal_reconcile_soon()
        self.assertTrue(hash_reconciler._wake_event.is_set())

        passes = []

        def fake_wait(timeout=None):
            passes.append(timeout)
            if len(passes) >= 2:
                raise _Sentinel()
            return True

        # Each monotonic() reading advances well past the signal floor, so the
        # signal is acted on rather than absorbed.
        clock = itertools.count(0, _FLOOR + 100)

        with patch.object(hash_reconciler.time, "monotonic", side_effect=lambda: next(clock)):
            with patch.object(hash_reconciler._wake_event, "wait", side_effect=fake_wait):
                with self.assertRaises(_Sentinel):
                    hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        self.assertEqual(service.reconcile_hashes.call_count, 2)
        self.assertEqual(passes[0], 360 * 60, "the wait must use the configured interval")
        hash_reconciler._wake_event.clear()

    def test_signal_too_soon_after_a_pass_is_absorbed(self):
        """A hash that can never resolve is re-signalled on every device poll.

        Each pass walks the whole catalogue, so acting on every signal would pin the
        reconciler at 100% duty for as long as one such book exists.
        """
        os.environ["KOSYNC_HASH_RECONCILE_ENABLED"] = "true"
        os.environ["KOSYNC_HASH_RECONCILE_MINUTES"] = "360"
        service = MagicMock()
        hash_reconciler._wake_event.clear()
        hash_reconciler.signal_reconcile_soon()

        waits = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            if len(waits) >= 3:
                raise _Sentinel()
            return True  # a signal is pending every time

        # Barely any time passes between readings, so every signal lands inside
        # the floor and must be absorbed.
        clock = itertools.count(0, 1)

        with patch.object(hash_reconciler.time, "monotonic", side_effect=lambda: next(clock)):
            with patch.object(hash_reconciler._wake_event, "wait", side_effect=fake_wait):
                with self.assertRaises(_Sentinel):
                    hash_reconciler.run_hash_reconciler_daemon(service, initial_delay_sec=0)

        self.assertEqual(
            service.reconcile_hashes.call_count, 1,
            "a signal inside the floor must not trigger another catalogue pass",
        )
        self.assertLessEqual(
            waits[1], _FLOOR,
            "the re-wait must be capped by the remaining cooldown",
        )
        hash_reconciler._wake_event.clear()


class TestPerUserScoping(unittest.TestCase):
    """The global bundle holds only the admin's credentials.

    Another user's CWA/BookOrbit books can never be revalidated by it, so each
    active user needs a pass with their own clients (CLAUDE.md failure mode #5).
    """

    def setUp(self):
        _reset_user_services()
        self.addCleanup(_reset_user_services)

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.global_service = KOReaderDeviceSyncService(
            database_service=MagicMock(),
            ebook_parser=MagicMock(),
            abs_client=MagicMock(),
            booklore_client=MagicMock(),
            cwa_client=MagicMock(),
            kavita_client=MagicMock(),
            epub_cache_dir=Path(self.tmp.name),
            bookorbit_client=MagicMock(),
        )
        self.global_service.reconcile_hashes = MagicMock(return_value={})

        self.bundle = SimpleNamespace(
            abs_client="user-abs",
            booklore_client="user-booklore",
            cwa_client="user-cwa",
            bookorbit_client="user-bookorbit",
        )
        self.registry = MagicMock()
        self.registry.get_clients.return_value = self.bundle

        self.db = MagicMock()
        self.db.list_users.return_value = [
            SimpleNamespace(id=1, active=1),
            SimpleNamespace(id=2, active=1),
        ]

    def test_scoped_service_uses_the_users_clients_and_books(self):
        scoped = hash_reconciler._scoped_service(self.global_service, self.registry, 2)

        self.assertEqual(scoped.user_id, 2)
        self.assertEqual(scoped.cwa_client, "user-cwa")
        self.assertEqual(scoped.bookorbit_client, "user-bookorbit")
        # Catalogue services stay shared.
        self.assertIs(scoped.database_service, self.global_service.database_service)
        self.assertIs(scoped.ebook_parser, self.global_service.ebook_parser)

    def test_scoped_service_shares_the_hash_cache(self):
        """A content hash is user-independent; re-hashing per user is pure waste."""
        scoped = hash_reconciler._scoped_service(self.global_service, self.registry, 2)

        self.assertIs(scoped._content_hash_cache, self.global_service._content_hash_cache)
        self.assertIs(scoped._content_hash_cache_lock, self.global_service._content_hash_cache_lock)

        # A hash computed under one scope is visible to the other.
        scoped._content_hash_cache["/books/x.epub"] = (1.0, 10, "deadbeef")
        self.assertEqual(
            self.global_service._content_hash_cache["/books/x.epub"], (1.0, 10, "deadbeef")
        )

    def test_user_scoped_service_only_sees_that_users_books(self):
        db = MagicMock()
        db.get_books_by_status.return_value = []
        scoped = KOReaderDeviceSyncService(
            database_service=db, ebook_parser=MagicMock(), abs_client=MagicMock(),
            booklore_client=MagicMock(), cwa_client=MagicMock(),
            epub_cache_dir=Path(self.tmp.name), user_id=7,
        )
        scoped._get_active_books()
        db.get_books_by_status.assert_called_once_with("active", user_id=7)

    def test_global_service_still_sees_every_book(self):
        db = MagicMock()
        db.get_books_by_status.return_value = []
        glob = KOReaderDeviceSyncService(
            database_service=db, ebook_parser=MagicMock(), abs_client=MagicMock(),
            booklore_client=MagicMock(), cwa_client=MagicMock(),
            epub_cache_dir=Path(self.tmp.name),
        )
        glob._get_active_books()
        db.get_books_by_status.assert_called_once_with("active")

    def test_a_pass_runs_for_every_active_user_plus_the_global_sweep(self):
        made = {}

        def fake_scoped(global_service, registry, user_id):
            svc = MagicMock()
            svc.reconcile_hashes.return_value = {}
            made[user_id] = svc
            return svc

        with patch.object(hash_reconciler, "_scoped_service", side_effect=fake_scoped):
            hash_reconciler._reconcile_all_users(self.global_service, self.registry, self.db)

        self.assertEqual(sorted(made), [1, 2])
        for svc in made.values():
            svc.reconcile_hashes.assert_called_once()
        self.global_service.reconcile_hashes.assert_called_once()

    def test_inactive_users_are_skipped(self):
        self.db.list_users.return_value = [
            SimpleNamespace(id=1, active=1),
            SimpleNamespace(id=2, active=0),
        ]
        made = {}

        def fake_scoped(global_service, registry, user_id):
            svc = MagicMock()
            made[user_id] = svc
            return svc

        with patch.object(hash_reconciler, "_scoped_service", side_effect=fake_scoped):
            hash_reconciler._reconcile_all_users(self.global_service, self.registry, self.db)

        self.assertEqual(sorted(made), [1])

    def test_ambient_user_is_bound_during_the_pass_and_reset_after(self):
        from src.utils.user_context import get_current_user_id
        seen = []

        def fake_scoped(global_service, registry, user_id):
            svc = MagicMock()
            svc.reconcile_hashes.side_effect = lambda: seen.append(get_current_user_id())
            return svc

        with patch.object(hash_reconciler, "_scoped_service", side_effect=fake_scoped):
            hash_reconciler._reconcile_all_users(self.global_service, self.registry, self.db)

        self.assertEqual(seen, [1, 2], "each pass must run under its own user context")
        self.assertIsNone(get_current_user_id(), "context must not leak past the pass")

    def test_one_users_failure_does_not_stop_the_others(self):
        def fake_scoped(global_service, registry, user_id):
            if user_id == 1:
                raise RuntimeError("credentials unavailable")
            svc = MagicMock()
            svc.reconcile_hashes.return_value = {}
            return svc

        with patch.object(hash_reconciler, "_scoped_service", side_effect=fake_scoped):
            hash_reconciler._reconcile_all_users(self.global_service, self.registry, self.db)

        self.global_service.reconcile_hashes.assert_called_once()

    def test_single_user_install_falls_back_to_the_global_sweep(self):
        with patch.object(hash_reconciler, "_scoped_service") as scoped:
            hash_reconciler._reconcile_all_users(self.global_service, None, None)

        scoped.assert_not_called()
        self.global_service.reconcile_hashes.assert_called_once()

    def test_per_user_services_are_rebuilt_every_pass(self):
        """A cached service would pin stale credentials until the next restart.

        Caching bought nothing for hashing — _scoped_service already shares the
        global service's file-hash cache — but it did mean a user who corrected a
        wrong ABS key or CWA password kept being reconciled with the old clients.
        """
        calls = []

        def fake_scoped(global_service, registry, user_id):
            calls.append(user_id)
            svc = MagicMock()
            svc.reconcile_hashes.return_value = {}
            return svc

        with patch.object(hash_reconciler, "_scoped_service", side_effect=fake_scoped):
            hash_reconciler._reconcile_all_users(self.global_service, self.registry, self.db)
            hash_reconciler._reconcile_all_users(self.global_service, self.registry, self.db)

        self.assertEqual(
            calls, [1, 2, 1, 2],
            "each pass must rebuild per-user clients so credential changes apply",
        )


if __name__ == "__main__":
    unittest.main()
