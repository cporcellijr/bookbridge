import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask, g

from src.api import goodreads_routes


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
