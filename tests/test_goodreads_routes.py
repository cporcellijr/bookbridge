import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask, g

from src.api import goodreads_routes
from src.db.database_service import DatabaseService
from src.db.models import Book, State


class TestGoodreadsRoutes(unittest.TestCase):
    def setUp(self):
        self.old_db = goodreads_routes._database_service
        self.old_container = goodreads_routes._container
        self.addCleanup(self._restore)
        self.db = Mock()
        self.db.get_book.return_value = SimpleNamespace(abs_id="a1", abs_title="Example Book")
        self.db.is_user_linked.return_value = True
        self.db.get_goodreads_details.return_value = None
        self.goodreads = Mock()
        self.goodreads.is_configured.return_value = True
        self.goodreads.book_url.return_value = "https://www.goodreads.com/book/show/123"
        self.goodreads.get_book.return_value = {
            "book_id": "123", "title": "Example Book", "author": "Example Author",
            "pages": 320, "rating": 4.2, "review_count": 120,
        }
        self.sync = Mock()
        self.sync._match_metadata.return_value = ("Example Book", "Example Author", "isbn", "")
        bundle = SimpleNamespace(
            goodreads_client=self.goodreads, sync_clients={"Goodreads": self.sync},
        )
        self.container = Mock()
        self.container.user_client_registry.return_value.get_clients.return_value = bundle
        goodreads_routes.init_goodreads_routes(self.db, self.container)
        app = Flask(__name__)
        app.register_blueprint(goodreads_routes.goodreads_bp)
        app.before_request(lambda: setattr(g, "current_user", SimpleNamespace(id=7, is_admin=False)))
        self.client = app.test_client()

    def _restore(self):
        goodreads_routes._database_service = self.old_db
        goodreads_routes._container = self.old_container

    def test_unclaimed_book_cannot_be_resolved_or_linked(self):
        self.db.is_user_linked.return_value = False
        self.assertEqual(self.client.get("/api/goodreads/resolve?abs_id=a1").status_code, 403)
        self.assertEqual(self.client.post("/link-goodreads/a1", json={"book_id": "123"}).status_code, 403)
        self.container.user_client_registry.assert_not_called()
        self.db.save_goodreads_details.assert_not_called()

    def test_resolve_uses_current_users_client(self):
        self.goodreads.resolve_book.return_value = {"book_id": "123", "title": "Example Book"}
        response = self.client.get("/api/goodreads/resolve?abs_id=a1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["book_id"], "123")
        self.goodreads.resolve_book.assert_called_once_with("Example Book", "Example Author", "isbn")
        self.container.goodreads_client.assert_not_called()

    def test_manual_link_saves_metadata_without_posting_shelf_or_progress(self):
        response = self.client.post("/link-goodreads/a1", json={"book_id": "123"})
        self.assertEqual(response.status_code, 200)
        details = self.db.save_goodreads_details.call_args.args[0]
        self.assertEqual((details.goodreads_book_id, details.matched_by), ("123", "manual"))
        self.assertEqual(details.goodreads_rating, 4.2)
        self.goodreads.set_shelf.assert_not_called()
        self.goodreads.update_progress.assert_not_called()
        self.container.goodreads_client.assert_not_called()

    def test_invalid_book_id_rejected_before_remote_read(self):
        response = self.client.post("/link-goodreads/a1", json={"book_id": "not-an-id"})
        self.assertEqual(response.status_code, 400)
        self.goodreads.get_book.assert_not_called()

    def test_first_link_clears_nothing(self):
        response = self.client.post("/link-goodreads/a1", json={"book_id": "123"})
        self.assertEqual(response.status_code, 200)
        self.db.delete_goodreads_details.assert_not_called()
        self.db.delete_state.assert_not_called()

    def test_relink_to_different_book_clears_old_details_and_posted_state_first(self):
        self.db.get_goodreads_details.return_value = SimpleNamespace(goodreads_book_id="999")
        with self.assertLogs("src.api.goodreads_routes", level="INFO") as logs:
            response = self.client.post("/link-goodreads/a1", json={"book_id": "123"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Goodreads: relinked 'Example Book' from 999 to 123; cleared posted progress",
                      " ".join(logs.output))
        self.db.delete_goodreads_details.assert_called_once_with("a1")
        self.db.delete_state.assert_called_once_with("a1", "goodreads")
        names = [call[0] for call in self.db.method_calls]
        self.assertLess(names.index("delete_state"), names.index("save_goodreads_details"))
        self.assertLess(names.index("delete_goodreads_details"), names.index("save_goodreads_details"))
        self.assertEqual(self.db.save_goodreads_details.call_args.args[0].goodreads_book_id, "123")

    def test_relink_to_same_book_keeps_posted_state(self):
        for existing_id in ("123", 123):
            with self.subTest(existing_id=existing_id):
                self.db.reset_mock()
                self.db.get_goodreads_details.return_value = SimpleNamespace(goodreads_book_id=existing_id)
                response = self.client.post("/link-goodreads/a1", json={"book_id": "123"})
                self.assertEqual(response.status_code, 200)
                self.db.delete_goodreads_details.assert_not_called()
                self.db.delete_state.assert_not_called()
                self.db.save_goodreads_details.assert_called_once()

    def test_failed_goodreads_lookup_leaves_existing_link_and_state_alone(self):
        self.db.get_goodreads_details.return_value = SimpleNamespace(goodreads_book_id="999")
        self.goodreads.get_book.return_value = None
        response = self.client.post("/link-goodreads/a1", json={"book_id": "123"})
        self.assertEqual(response.status_code, 404)
        self.db.delete_goodreads_details.assert_not_called()
        self.db.delete_state.assert_not_called()

    def test_resolve_skips_second_fetch_when_match_is_already_a_full_book(self):
        full = {"book_id": "123", "title": "Example Book", "author": "Example Author",
                "pages": 320, "rating": 4.2, "review_count": 120, "shelf": None}
        self.goodreads.resolve_book_from_input.return_value = full
        response = self.client.get("/api/goodreads/resolve?abs_id=a1&input=123")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json["pages"], response.json["rating"], response.json["review_count"]),
                         (320, 4.2, 120))
        self.goodreads.get_book.assert_not_called()

    def test_resolve_skips_second_fetch_for_full_book_with_unknown_page_count(self):
        self.goodreads.resolve_book_from_input.return_value = {
            "book_id": "123", "title": "Example Book", "author": "Example Author", "pages": None,
        }
        response = self.client.get("/api/goodreads/resolve?abs_id=a1&input=123")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json["pages"])
        self.goodreads.get_book.assert_not_called()

    def test_resolve_fetches_full_book_for_search_cards(self):
        self.goodreads.resolve_book.return_value = {
            "book_id": "123", "title": "Example Book", "author": "Example Author",
            "url": "https://www.goodreads.com/book/show/123",
        }
        response = self.client.get("/api/goodreads/resolve?abs_id=a1")
        self.assertEqual(response.status_code, 200)
        self.goodreads.get_book.assert_called_once_with("123")
        self.assertEqual(response.json["pages"], 320)

    def test_resolve_of_saved_link_reads_the_book_once(self):
        self.db.get_goodreads_details.return_value = SimpleNamespace(goodreads_book_id="123")
        response = self.client.get("/api/goodreads/resolve?abs_id=a1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["linked"])
        self.goodreads.get_book.assert_called_once_with("123")


class TestDeleteState(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = DatabaseService(str(Path(self.temp.name) / "delete_state.db"))
        self.db.save_book(Book(abs_id="a1", abs_title="Example", status="active"))
        for user_id in (1, 2):
            for client_name in ("goodreads", "storygraph"):
                self.db.save_state(State(abs_id="a1", client_name=client_name, user_id=user_id,
                                         last_updated=1.0, percentage=0.5))

    def tearDown(self) -> None:
        self.db.db_manager.close()
        self.temp.cleanup()

    def test_deletes_only_that_users_row_for_that_client(self) -> None:
        self.assertTrue(self.db.delete_state("a1", "goodreads", user_id=1))
        self.assertIsNone(self.db.get_state("a1", "goodreads", user_id=1))
        self.assertIsNotNone(self.db.get_state("a1", "goodreads", user_id=2))
        self.assertIsNotNone(self.db.get_state("a1", "storygraph", user_id=1))
        self.assertFalse(self.db.delete_state("a1", "goodreads", user_id=1))

    def test_defaults_to_the_ambient_user_like_get_state(self) -> None:
        from src.utils.user_context import reset_current_user_id, set_current_user_id

        token = set_current_user_id(2)
        try:
            self.assertTrue(self.db.delete_state("a1", "goodreads"))
        finally:
            reset_current_user_id(token)
        self.assertIsNone(self.db.get_state("a1", "goodreads", user_id=2))
        self.assertIsNotNone(self.db.get_state("a1", "goodreads", user_id=1))


class TestGoodreadsConnectionCheck(unittest.TestCase):
    def test_tests_cookie_without_global_gate_or_saveback(self):
        from src.web_server import _test_goodreads

        with patch("src.api.goodreads_client.GoodreadsClient.check_connection", autospec=True,
                   side_effect=lambda client: client.is_configured()) as check:
            with patch.dict("os.environ", {"GOODREADS_ENABLED": "false"}):
                result = _test_goodreads(True, "session=user", "", "")
        self.assertTrue(result["ok"])
        check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
