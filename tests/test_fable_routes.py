"""Fable book-linking routes (mirrors the Goodreads route tests)."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, g

from src.api import fable_routes

BOOK_ID = "0a1b2c3d-1111-4222-8333-444455556666"
OTHER_ID = "9f8e7d6c-1111-4222-8333-444455556666"


class TestFableRoutes(unittest.TestCase):
    def setUp(self):
        self.old_db = fable_routes._database_service
        self.old_container = fable_routes._container
        self.addCleanup(self._restore)
        self.db = Mock()
        self.db.get_book.return_value = SimpleNamespace(abs_id="a1", abs_title="Example Book")
        self.db.is_user_linked.return_value = True
        self.db.get_fable_details.return_value = None
        self.fable = Mock()
        self.fable.is_configured.return_value = True
        self.fable.book_url.return_value = "https://fable.co/book/example-book"
        self.fable.get_book.return_value = {
            "book_id": BOOK_ID, "title": "Example Book", "author": "Example Author",
            "pages": 320, "rating": 4.2, "review_count": 120,
        }
        self.sync = Mock()
        self.sync._match_metadata.return_value = ("Example Book", "Example Author", "isbn", "")
        bundle = SimpleNamespace(
            fable_client=self.fable, sync_clients={"Fable": self.sync},
        )
        self.container = Mock()
        self.container.user_client_registry.return_value.get_clients.return_value = bundle
        fable_routes.init_fable_routes(self.db, self.container)
        app = Flask(__name__)
        app.register_blueprint(fable_routes.fable_bp)
        app.before_request(lambda: setattr(g, "current_user", SimpleNamespace(id=7, is_admin=False)))
        self.client = app.test_client()

    def _restore(self):
        fable_routes._database_service = self.old_db
        fable_routes._container = self.old_container

    def test_unclaimed_book_cannot_be_resolved_or_linked(self):
        self.db.is_user_linked.return_value = False
        self.assertEqual(self.client.get("/api/fable/resolve?abs_id=a1").status_code, 403)
        self.assertEqual(self.client.post("/link-fable/a1", json={"book_id": BOOK_ID}).status_code, 403)
        self.container.user_client_registry.assert_not_called()
        self.db.save_fable_details.assert_not_called()

    def test_resolve_uses_current_users_client(self):
        self.fable.resolve_book.return_value = {"book_id": BOOK_ID, "title": "Example Book"}
        response = self.client.get("/api/fable/resolve?abs_id=a1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["book_id"], BOOK_ID)
        self.fable.resolve_book.assert_called_once_with("Example Book", "Example Author", "isbn")
        self.container.fable_client.assert_not_called()

    def test_manual_link_saves_metadata_without_posting_shelf_or_progress(self):
        response = self.client.post("/link-fable/a1", json={"book_id": BOOK_ID})
        self.assertEqual(response.status_code, 200)
        details = self.db.save_fable_details.call_args.args[0]
        self.assertEqual((details.fable_book_id, details.matched_by), (BOOK_ID, "manual"))
        self.assertEqual(details.fable_rating, 4.2)
        self.fable.set_shelf.assert_not_called()
        self.fable.update_progress.assert_not_called()
        self.container.fable_client.assert_not_called()

    def test_invalid_book_id_rejected_before_remote_read(self):
        response = self.client.post("/link-fable/a1", json={"book_id": "not-an-id"})
        self.assertEqual(response.status_code, 400)
        self.fable.get_book.assert_not_called()

    def test_first_link_clears_nothing(self):
        response = self.client.post("/link-fable/a1", json={"book_id": BOOK_ID})
        self.assertEqual(response.status_code, 200)
        self.db.delete_fable_details.assert_not_called()
        self.db.delete_state.assert_not_called()

    def test_relink_to_different_book_clears_old_details_and_posted_state_first(self):
        self.db.get_fable_details.return_value = SimpleNamespace(fable_book_id=OTHER_ID)
        with self.assertLogs("src.api.fable_routes", level="INFO") as logs:
            response = self.client.post("/link-fable/a1", json={"book_id": BOOK_ID})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Fable: relinked 'Example Book' from {OTHER_ID} to {BOOK_ID}; cleared posted progress",
                      " ".join(logs.output))
        self.db.delete_fable_details.assert_called_once_with("a1")
        self.db.delete_state.assert_called_once_with("a1", "fable")
        names = [call[0] for call in self.db.method_calls]
        self.assertLess(names.index("delete_state"), names.index("save_fable_details"))
        self.assertLess(names.index("delete_fable_details"), names.index("save_fable_details"))
        self.assertEqual(self.db.save_fable_details.call_args.args[0].fable_book_id, BOOK_ID)

    def test_relink_to_same_book_keeps_posted_state(self):
        for existing_id in (BOOK_ID,):
            with self.subTest(existing_id=existing_id):
                self.db.reset_mock()
                self.db.get_fable_details.return_value = SimpleNamespace(fable_book_id=existing_id)
                response = self.client.post("/link-fable/a1", json={"book_id": BOOK_ID})
                self.assertEqual(response.status_code, 200)
                self.db.delete_fable_details.assert_not_called()
                self.db.delete_state.assert_not_called()
                self.db.save_fable_details.assert_called_once()

    def test_failed_fable_lookup_leaves_existing_link_and_state_alone(self):
        self.db.get_fable_details.return_value = SimpleNamespace(fable_book_id=OTHER_ID)
        self.fable.get_book.return_value = None
        response = self.client.post("/link-fable/a1", json={"book_id": BOOK_ID})
        self.assertEqual(response.status_code, 404)
        self.db.delete_fable_details.assert_not_called()
        self.db.delete_state.assert_not_called()

    def test_resolve_skips_second_fetch_when_match_is_already_a_full_book(self):
        full = {"book_id": BOOK_ID, "title": "Example Book", "author": "Example Author",
                "pages": 320, "rating": 4.2, "review_count": 120, "shelf": None}
        self.fable.resolve_book_from_input.return_value = full
        response = self.client.get(f"/api/fable/resolve?abs_id=a1&input={BOOK_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json["pages"], response.json["rating"], response.json["review_count"]),
                         (320, 4.2, 120))
        self.fable.get_book.assert_not_called()

    def test_resolve_skips_second_fetch_for_full_book_with_unknown_page_count(self):
        self.fable.resolve_book_from_input.return_value = {
            "book_id": BOOK_ID, "title": "Example Book", "author": "Example Author", "pages": None,
        }
        response = self.client.get(f"/api/fable/resolve?abs_id=a1&input={BOOK_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json["pages"])
        self.fable.get_book.assert_not_called()

    def test_resolve_fetches_full_book_for_search_cards(self):
        self.fable.resolve_book.return_value = {
            "book_id": BOOK_ID, "title": "Example Book", "author": "Example Author",
            "url": "https://fable.co/book/example-book",
        }
        response = self.client.get("/api/fable/resolve?abs_id=a1")
        self.assertEqual(response.status_code, 200)
        self.fable.get_book.assert_called_once_with(BOOK_ID)
        self.assertEqual(response.json["pages"], 320)

    def test_resolve_of_saved_link_reads_the_book_once(self):
        self.db.get_fable_details.return_value = SimpleNamespace(fable_book_id=BOOK_ID)
        response = self.client.get("/api/fable/resolve?abs_id=a1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["linked"])
        self.fable.get_book.assert_called_once_with(BOOK_ID)

    def test_link_saves_the_canonical_fable_url(self):
        self.fable.get_book.return_value = {**self.fable.get_book.return_value, "url": "https://fable.co/book/canonical"}
        self.client.post("/link-fable/a1", json={"book_id": BOOK_ID})
        self.assertEqual(self.db.save_fable_details.call_args.args[0].fable_url, "https://fable.co/book/canonical")

    def test_uppercase_book_id_is_accepted(self):
        response = self.client.post("/link-fable/a1", json={"book_id": BOOK_ID.upper()})
        self.assertEqual(response.status_code, 200)
        self.fable.get_book.assert_called_once_with(BOOK_ID)


if __name__ == "__main__":
    unittest.main()
