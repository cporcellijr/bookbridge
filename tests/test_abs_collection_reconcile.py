"""Matched audiobooks are kept in their owner's ABS auto-add collection.

The collection used to be filled only by the one add made when a book is
matched, so an add that failed then (ABS briefly unreachable, a busy daemon)
left the book out for good. The reconcile re-adds whatever is missing each
sync cycle.
"""

import logging
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.web_server as web_server
from src.api.api_clients import ABSClient
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY, user_setting
from src.utils.user_context import reset_current_user_credentials, set_current_user_credentials

COLLECTIONS_URL = "http://abs.example/api/collections"


def _book(abs_id, user_id=1, audio_source="ABS", sync_mode="audiobook"):
    return SimpleNamespace(abs_id=abs_id, abs_title=abs_id, audio_source=audio_source,
                           sync_mode=sync_mode, user_id=user_id)


def _response(status, payload=None, text=""):
    resp = MagicMock(status_code=status, text=text)
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status} Error")
    return resp


def _collection(collection_id, library_id, *item_ids, name="Ebook Synced"):
    return {"id": collection_id, "libraryId": library_id, "name": name,
            "books": [{"id": item_id, "libraryId": library_id} for item_id in item_ids]}


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db._default_user_id.return_value = 1
        self.bundles = {}
        registry = Mock()
        registry.get_clients.side_effect = self.bundle
        container = Mock()
        container.user_client_registry.return_value = registry
        for patcher in (
            patch.object(web_server, "database_service", self.db),
            patch.object(web_server, "container", container),
            patch.dict(os.environ, {"ABS_COLLECTION_RECONCILE": "true"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("ABS_COLLECTION_NAME", None)
        get_persistent_condition_logger().reset()
        self.addCleanup(get_persistent_condition_logger().reset)

    def bundle(self, uid, collection="Ebook Synced", primary_admin=False):
        if uid not in self.bundles:
            client = Mock()
            client.is_configured.return_value = True
            client.add_missing_to_collection.return_value = (0, [])
            self.bundles[uid] = SimpleNamespace(
                abs_client=client,
                credentials={"ABS_COLLECTION_NAME": collection, _ALLOW_GLOBAL_FALLBACK_KEY: primary_admin},
            )
        return self.bundles[uid]

    def client(self, uid):
        return self.bundle(uid).abs_client

    def books(self, *books):
        self.db.get_books_by_status.return_value = list(books)


class TestAbsCollectionReconcile(ReconcileTestCase):
    def test_off_by_default_touches_nothing(self):
        os.environ.pop("ABS_COLLECTION_RECONCILE", None)
        self.books(_book("a"))

        self.assertIsNone(web_server._reconcile_abs_collection())
        self.db.get_books_by_status.assert_not_called()

    def test_true_and_on_both_enable_it(self):
        self.books(_book("a"))
        for spelling in ("true", "on"):
            with self.subTest(spelling=spelling), patch.dict(os.environ, {"ABS_COLLECTION_RECONCILE": spelling}):
                self.bundles.clear()

                web_server._reconcile_abs_collection()

                self.client(1).add_missing_to_collection.assert_called_once_with(["a"], "Ebook Synced")

    def test_hands_the_owners_audiobooks_to_its_client_and_counts_the_adds(self):
        self.client(1).add_missing_to_collection.return_value = (1, [])
        self.books(_book("a"), _book("b"))

        self.assertEqual(web_server._reconcile_abs_collection(), 1)
        self.client(1).add_missing_to_collection.assert_called_once_with(["a", "b"], "Ebook Synced")

    def test_books_without_abs_audio_are_left_alone(self):
        self.books(
            _book("booklore:12", audio_source="BookLore"),
            _book("bookorbit:7", audio_source="BookOrbit"),
            _book("ebook-0123456789abcdef", audio_source=None, sync_mode="ebook_only"),
            _book("booklore:99", audio_source=None),
        )

        self.assertEqual(web_server._reconcile_abs_collection(), 0)
        self.assertEqual(self.bundles, {})

    def test_legacy_rows_without_an_audio_source_count_as_abs(self):
        self.books(_book("legacy", audio_source=None))

        web_server._reconcile_abs_collection()

        self.client(1).add_missing_to_collection.assert_called_once_with(["legacy"], "Ebook Synced")

    def test_each_owner_uses_its_own_client_and_collection(self):
        self.bundle(12, collection="Shared Picks")
        self.books(_book("a", user_id=1), _book("b", user_id=12))

        web_server._reconcile_abs_collection()

        self.client(1).add_missing_to_collection.assert_called_once_with(["a"], "Ebook Synced")
        self.client(12).add_missing_to_collection.assert_called_once_with(["b"], "Shared Picks")

    def test_unowned_books_go_through_the_primary_admin(self):
        self.books(_book("a", user_id=None))

        web_server._reconcile_abs_collection()

        self.client(1).add_missing_to_collection.assert_called_once_with(["a"], "Ebook Synced")

    def test_collection_name_is_the_one_the_match_time_add_resolves(self):
        self.bundle(1, collection="", primary_admin=True)
        self.bundle(12, collection="")
        self.books(_book("a", user_id=1), _book("b", user_id=12))

        with patch.dict(os.environ, {"ABS_COLLECTION_NAME": "Global Shelf"}):
            web_server._reconcile_abs_collection()
            match_time_names = {}
            for uid in (1, 12):
                token = set_current_user_credentials(self.bundles[uid].credentials)
                try:
                    match_time_names[uid] = user_setting("ABS_COLLECTION_NAME", "Synced with KOReader")
                finally:
                    reset_current_user_credentials(token)

        self.assertEqual(match_time_names, {1: "Global Shelf", 12: "Synced with KOReader"})
        self.client(1).add_missing_to_collection.assert_called_once_with(["a"], "Global Shelf")
        self.client(12).add_missing_to_collection.assert_called_once_with(["b"], "Synced with KOReader")

    def test_an_unconfigured_owner_is_skipped(self):
        self.client(1).is_configured.return_value = False
        self.books(_book("a"))

        self.assertEqual(web_server._reconcile_abs_collection(), 0)
        self.client(1).add_missing_to_collection.assert_not_called()

    def test_one_owners_failure_does_not_skip_the_rest(self):
        self.client(1).add_missing_to_collection.side_effect = ValueError("not JSON")
        self.client(12).add_missing_to_collection.return_value = (1, [])
        self.books(_book("a", user_id=1), _book("b", user_id=12))

        self.assertEqual(web_server._reconcile_abs_collection(), 1)
        self.client(12).add_missing_to_collection.assert_called_once_with(["b"], "Ebook Synced")

    def test_a_failing_owner_warns_once_then_announces_recovery(self):
        self.client(1).add_missing_to_collection.side_effect = requests.ConnectionError("down")
        self.books(_book("a"))

        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            web_server._reconcile_abs_collection()
            web_server._reconcile_abs_collection()
            self.client(1).add_missing_to_collection.side_effect = None
            web_server._reconcile_abs_collection()

        failures = [r for r in logs.records if "reconcile failed for user 1" in r.getMessage()]
        self.assertEqual([r.levelno for r in failures], [logging.WARNING, logging.DEBUG])
        self.assertTrue(all(r.exc_info for r in failures))
        recoveries = [r.getMessage() for r in logs.records if "resumed for user 1" in r.getMessage()]
        self.assertEqual(len(recoveries), 1)
        self.assertIn("recovered after 2 occurrences", recoveries[0])

    def test_refused_adds_warn_once_then_announce_recovery(self):
        self.client(1).add_missing_to_collection.return_value = (1, ["b: add returned 403 - Forbidden"])
        self.books(_book("a"), _book("b"))

        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            self.assertEqual(web_server._reconcile_abs_collection(), 1)
            web_server._reconcile_abs_collection()
            self.client(1).add_missing_to_collection.return_value = (1, [])
            web_server._reconcile_abs_collection()

        refusals = [r for r in logs.records if "could not be added" in r.getMessage()]
        self.assertEqual([r.levelno for r in refusals], [logging.WARNING, logging.DEBUG])
        self.assertIn("b: add returned 403 - Forbidden", refusals[0].getMessage())
        recoveries = [r.getMessage() for r in logs.records if "is complete again" in r.getMessage()]
        self.assertEqual(len(recoveries), 1)
        self.assertIn("recovered after 2 occurrences", recoveries[0])

    def test_errors_are_contained(self):
        self.db.get_books_by_status.side_effect = RuntimeError("db gone")

        with self.assertLogs("src.web_server", level="DEBUG") as logs:
            self.assertIsNone(web_server._reconcile_abs_collection())
            self.assertIsNone(web_server._reconcile_abs_collection())

        self.assertEqual([r.levelno for r in logs.records], [logging.WARNING, logging.DEBUG])
        self.assertTrue(all(r.exc_info for r in logs.records))


class ABSClientTestCase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ABS_SERVER": "http://abs.example", "ABS_KEY": "token"})
        env.start()
        self.addCleanup(env.stop)
        self.client = ABSClient()
        self.client.session = MagicMock()
        self.collections = []
        self.item_libraries = {}
        self.client.session.get.side_effect = self._get
        self.client.session.post.return_value = _response(200, {})

    def _get(self, url, **kwargs):
        if url == COLLECTIONS_URL:
            return _response(200, {"collections": self.collections})
        if url == "http://abs.example/api/libraries":
            return _response(200, {"libraries": [{"id": "lib-a"}]})
        item_id = url.rsplit("/", 1)[-1]
        if item_id in self.item_libraries:
            return _response(200, {"id": item_id, "libraryId": self.item_libraries[item_id]})
        return _response(404)

    def assert_every_request_has_the_client_timeout(self):
        calls = self.client.session.get.call_args_list + self.client.session.post.call_args_list
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call.kwargs.get("timeout"), self.client.timeout, call)


class TestAddMissingToCollection(ABSClientTestCase):
    def test_items_already_in_the_collection_cost_one_request(self):
        self.collections = [_collection("col-a", "lib-a", "a", "b")]

        self.assertEqual(self.client.add_missing_to_collection(["a", "b"], "Ebook Synced"), (0, []))
        self.client.session.get.assert_called_once_with(COLLECTIONS_URL, timeout=self.client.timeout)
        self.client.session.post.assert_not_called()

    def test_a_missing_item_is_added_to_the_collection_of_its_own_library(self):
        self.collections = [_collection("col-a", "lib-a", "a"), _collection("col-b", "lib-b", "x")]
        self.item_libraries = {"b": "lib-b"}

        self.assertEqual(self.client.add_missing_to_collection(["a", "b"], "Ebook Synced"), (1, []))
        self.client.session.post.assert_called_once_with(
            f"{COLLECTIONS_URL}/col-b/book", json={"id": "b"}, timeout=self.client.timeout,
        )

    def test_an_item_in_another_librarys_collection_of_that_name_is_not_missing(self):
        self.collections = [_collection("col-a", "lib-a", "a"), _collection("col-b", "lib-b", "b")]

        self.assertEqual(self.client.add_missing_to_collection(["a", "b"], "Ebook Synced"), (0, []))
        self.client.session.get.assert_called_once()
        self.client.session.post.assert_not_called()

    def test_a_collection_of_another_name_is_ignored(self):
        self.collections = [_collection("col-o", "lib-a", "a", name="Other"), _collection("col-a", "lib-a")]
        self.item_libraries = {"a": "lib-a"}

        self.assertEqual(self.client.add_missing_to_collection(["a"], "Ebook Synced"), (1, []))
        self.client.session.post.assert_called_once_with(
            f"{COLLECTIONS_URL}/col-a/book", json={"id": "a"}, timeout=self.client.timeout,
        )

    def test_a_library_without_the_collection_gets_it_created_once_with_its_items(self):
        self.collections = [_collection("col-a", "lib-a", "a")]
        self.item_libraries = {"b": "lib-b", "c": "lib-b"}

        self.assertEqual(self.client.add_missing_to_collection(["a", "b", "c"], "Ebook Synced"), (2, []))
        self.client.session.post.assert_called_once_with(
            COLLECTIONS_URL,
            json={"libraryId": "lib-b", "name": "Ebook Synced", "books": ["b", "c"]},
            timeout=self.client.timeout,
        )

    def test_the_collections_are_listed_once_however_many_items_are_missing(self):
        self.collections = [_collection("col-a", "lib-a")]
        self.item_libraries = {"a": "lib-a", "b": "lib-a", "c": "lib-a"}

        self.assertEqual(self.client.add_missing_to_collection(["a", "b", "c"], "Ebook Synced"), (3, []))
        listings = [c for c in self.client.session.get.call_args_list if c.args[0] == COLLECTIONS_URL]
        self.assertEqual(len(listings), 1)
        self.assertEqual(self.client.session.get.call_count + self.client.session.post.call_count, 7)

    def test_every_request_has_a_timeout(self):
        self.collections = [_collection("col-a", "lib-a")]
        self.item_libraries = {"a": "lib-a", "b": "lib-b"}

        self.assertEqual(self.client.add_missing_to_collection(["a", "b"], "Ebook Synced"), (2, []))
        self.assertEqual(self.client.session.post.call_count, 2)
        self.assert_every_request_has_the_client_timeout()

    def test_a_failed_listing_raises_and_adds_nothing(self):
        self.client.session.get.side_effect = None
        self.client.session.get.return_value = _response(502)

        with self.assertRaises(requests.HTTPError):
            self.client.add_missing_to_collection(["a"], "Ebook Synced")
        self.client.session.post.assert_not_called()

    def test_a_listing_that_is_not_json_raises_and_adds_nothing(self):
        listing = _response(200)
        listing.json.side_effect = ValueError("Expecting value")
        self.client.session.get.side_effect = None
        self.client.session.get.return_value = listing

        with self.assertRaises(ValueError):
            self.client.add_missing_to_collection(["a"], "Ebook Synced")
        self.client.session.post.assert_not_called()

    def test_a_stalled_server_costs_a_single_timeout(self):
        self.collections = [_collection("col-a", "lib-a")]
        listing = self._get(COLLECTIONS_URL)
        self.client.session.get.side_effect = [listing, requests.Timeout("read timed out")]

        with self.assertRaises(requests.Timeout):
            self.client.add_missing_to_collection(["a", "b", "c"], "Ebook Synced")
        self.assertEqual(self.client.session.get.call_count, 2)
        self.client.session.post.assert_not_called()

    def test_a_refused_add_is_reported_and_the_rest_continue(self):
        self.collections = [_collection("col-a", "lib-a")]
        self.item_libraries = {"a": "lib-a", "b": "lib-a"}
        self.client.session.post.side_effect = [_response(403, text="Forbidden"), _response(200, {})]

        self.assertEqual(
            self.client.add_missing_to_collection(["a", "b"], "Ebook Synced"),
            (1, ["a: add returned 403 - Forbidden"]),
        )

    def test_a_refused_create_reports_every_item_of_that_library(self):
        self.item_libraries = {"a": "lib-a", "b": "lib-a"}
        self.client.session.post.return_value = _response(400, text="Invalid collection data")

        self.assertEqual(
            self.client.add_missing_to_collection(["a", "b"], "Ebook Synced"),
            (0, ["a: create returned 400 - Invalid collection data",
                 "b: create returned 400 - Invalid collection data"]),
        )
        self.client.session.post.assert_called_once()

    def test_an_item_abs_no_longer_has_is_reported_not_added(self):
        self.collections = [_collection("col-a", "lib-a")]

        self.assertEqual(
            self.client.add_missing_to_collection(["gone"], "Ebook Synced"),
            (0, ["gone: no library (item lookup returned 404)"]),
        )
        self.client.session.post.assert_not_called()

    def test_a_disabled_client_makes_no_request(self):
        with patch.dict(os.environ, {"ABS_SERVER": "disabled"}):
            self.assertEqual(self.client.add_missing_to_collection(["a"], "Ebook Synced"), (0, []))
        self.client.session.get.assert_not_called()


class TestAddToCollection(ABSClientTestCase):
    def test_every_request_has_a_timeout_when_the_collection_exists(self):
        self.collections = [_collection("col-a", "lib-a")]
        self.item_libraries = {"a": "lib-a"}

        self.assertTrue(self.client.add_to_collection("a", "Ebook Synced"))
        self.client.session.post.assert_called_once()
        self.assert_every_request_has_the_client_timeout()

    def test_every_request_has_a_timeout_when_the_collection_is_created(self):
        self.assertTrue(self.client.add_to_collection("unknown-item", "Ebook Synced"))
        self.client.session.post.assert_called_once()
        self.assertIn(
            "http://abs.example/api/libraries", [c.args[0] for c in self.client.session.get.call_args_list],
        )
        self.assert_every_request_has_the_client_timeout()

    def test_a_rejected_add_is_logged(self):
        self.collections = [_collection("col-a", "lib-a")]
        self.client.session.post.return_value = _response(403, text="forbidden")

        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            self.assertFalse(self.client.add_to_collection("item-1", "Ebook Synced"))
        self.assertIn("add returned 403", "\n".join(logs.output))

    def test_a_failed_listing_is_logged(self):
        self.client.session.get.side_effect = None
        self.client.session.get.return_value = _response(502)

        with self.assertLogs("src.api.api_clients", level="WARNING") as logs:
            self.assertFalse(self.client.add_to_collection("item-1", "Ebook Synced"))
        self.assertIn("listing collections returned 502", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
