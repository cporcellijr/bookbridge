"""Fable client: sign-in, book lookups, lists and progress, against mocked HTTP."""

import unittest
from unittest.mock import Mock, patch

from src.api import fable_client
from src.api.fable_client import FableClient

BOOK_ID = "0a1b2c3d-1111-4222-8333-444455556666"
OTHER_ID = "9f8e7d6c-1111-4222-8333-444455556666"


def _resp(status=200, json=None):
    resp = Mock(status_code=status, content=b"{}" if json is not None else b"")
    resp.json.return_value = json if json is not None else {}
    return resp


def _book(book_id=BOOK_ID, status=None, **extra):
    return {"id": book_id, "title": "Example Book", "authors": [{"name": "Example Author"}],
            "isbn": "9780000000002", "page_count": 320, "review_average": 4.1, "review_count": 120,
            "url": "https://fable.co/book/example-book-by-example-author-9780000000002", "status": status, **extra}


SIGN_IN = {"idToken": "id-1", "refreshToken": "refresh-1", "expiresIn": "3600"}
LISTS = {"results": [
    {"id": "list-want", "type": "system", "system_type": "want_to_read"},
    {"id": "list-current", "type": "system", "system_type": "current_reading"},
    {"id": "list-finished", "type": "system", "system_type": "finished"},
    {"id": "list-dnf", "type": "system", "system_type": "did_not_finish"},
    {"id": "list-mine", "type": "custom"},
]}


class FableClientTestCase(unittest.TestCase):
    def setUp(self):
        fable_client._SESSIONS.clear()
        self.addCleanup(fable_client._SESSIONS.clear)
        self.client = FableClient({"FABLE_ENABLED": "true", "FABLE_EMAIL": "reader@example.com",
                                   "FABLE_PASSWORD": "secret"})
        post = patch("src.api.fable_client.requests.post")
        request = patch("src.api.fable_client.requests.request")
        self.firebase = post.start()
        self.api = request.start()
        self.addCleanup(post.stop)
        self.addCleanup(request.stop)
        self.firebase.return_value = _resp(json=SIGN_IN)
        self.routes = {("GET", "/api/settings/profile/"): _resp(json={"id": 42})}
        self.calls = []

        def route(method, url, json=None, **kwargs):
            path = url.replace(fable_client.FABLE_API, "")
            self.calls.append((method, path, json))
            for (m, prefix), resp in self.routes.items():
                if m == method and path.startswith(prefix):
                    return resp
            return _resp(404)
        self.api.side_effect = route


class TestSignIn(FableClientTestCase):
    def test_not_configured_without_email_or_password(self):
        self.assertFalse(FableClient({"FABLE_ENABLED": "true", "FABLE_EMAIL": "x@example.com"}).is_configured())
        self.assertFalse(FableClient({"FABLE_ENABLED": "false", "FABLE_EMAIL": "x", "FABLE_PASSWORD": "y"}).is_configured())
        self.assertTrue(self.client.is_configured())

    def test_signs_in_once_and_reuses_the_session_across_clients(self):
        self.assertTrue(self.client.check_connection())
        again = FableClient({"FABLE_ENABLED": "true", "FABLE_EMAIL": "Reader@example.com", "FABLE_PASSWORD": "secret"})
        self.routes[("GET", "/api/books/")] = _resp(json={"response": _book()})
        self.assertEqual(again.get_book(BOOK_ID)["title"], "Example Book")
        self.assertEqual(self.firebase.call_count, 1)  # the rebuilt client reused the session
        auth = self.api.call_args.kwargs["headers"]["Authorization"]
        self.assertEqual(auth, "JWT id-1")

    def test_expired_session_is_renewed_with_the_refresh_token(self):
        self.client.check_connection()
        fable_client._SESSIONS[self.client._login_key()]["expires_at"] = 0
        self.firebase.return_value = _resp(json={"id_token": "id-2", "refresh_token": "refresh-2", "expires_in": "3600"})
        self.routes[("GET", "/api/books/")] = _resp(json={"response": _book()})
        self.client.get_book(BOOK_ID)
        url = self.firebase.call_args.args[0]
        self.assertTrue(url.startswith(fable_client._FIREBASE_REFRESH_URL))
        self.assertEqual(self.firebase.call_args.kwargs["data"]["refresh_token"], "refresh-1")
        self.assertEqual(self.api.call_args.kwargs["headers"]["Authorization"], "JWT id-2")

    def test_a_401_renews_the_session_once_and_retries(self):
        self.client.check_connection()
        responses = iter([_resp(401), _resp(json={"response": _book()})])
        self.api.side_effect = lambda *a, **k: next(responses)
        self.firebase.return_value = _resp(json={"id_token": "id-2", "refresh_token": "refresh-1", "expires_in": "3600"})
        self.assertIsNotNone(self.client.get_book(BOOK_ID))
        self.assertEqual(self.api.call_args.kwargs["headers"]["Authorization"], "JWT id-2")

    def test_rejected_login_is_not_retried_for_a_while(self):
        self.firebase.return_value = _resp(400, json={"error": {"message": "INVALID_PASSWORD"}})
        with self.assertLogs("src.api.fable_client", "WARNING"):
            self.assertIsNone(self.client.get_book(BOOK_ID))
        self.assertIsNone(self.client.get_book(BOOK_ID))
        self.assertEqual(self.firebase.call_count, 1)
        self.api.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.client.check_connection()  # an explicit test tries again


class TestBooks(FableClientTestCase):
    def test_book_carries_metadata_rating_and_your_list(self):
        self.routes[("GET", f"/api/books/{BOOK_ID}")] = _resp(json={"response": _book(status="current_reading")})
        book = self.client.get_book(BOOK_ID)
        self.assertEqual((book["book_id"], book["author"], book["pages"], book["rating"], book["review_count"]),
                         (BOOK_ID, "Example Author", 320, 4.1, 120))
        self.assertEqual(book["shelf"], "current_reading")
        self.assertTrue(book["url"].startswith("https://fable.co/book/"))

    def test_invalid_ids_are_not_requested(self):
        self.assertIsNone(self.client.get_book("123"))
        self.assertFalse(self.client.set_shelf("123", "finished"))
        self.assertFalse(self.client.update_progress("../x", 0.5))
        self.api.assert_not_called()

    def test_resolve_from_id_prefixed_id_url_and_text(self):
        self.routes[("GET", f"/api/books/{BOOK_ID}")] = _resp(json={"response": _book()})
        results = [_book(OTHER_ID, url="https://fable.co/book/other"), _book()]
        self.routes[("GET", "/api/books/search/")] = _resp(json={"response": {"books": results}})
        self.assertEqual(self.client.resolve_book_from_input(BOOK_ID)["book_id"], BOOK_ID)
        self.assertEqual(self.client.resolve_book_from_input(f"fable:{BOOK_ID}")["book_id"], BOOK_ID)
        url = "https://fable.co/book/example-book-by-example-author-9780000000002"
        self.assertEqual(self.client.resolve_book_from_input(url)["book_id"], BOOK_ID)  # matches the URL, not the first hit
        self.assertIn("auto=9780000000002", self.calls[-1][1])  # searched by the URL's ISBN
        self.assertEqual(self.client.resolve_book_from_input("example book")["book_id"], OTHER_ID)

    def test_resolve_book_rejects_another_author(self):
        self.routes[("GET", "/api/books/search/")] = _resp(json={"response": {"books": [
            _book(authors=[{"name": "Somebody Else"}])]}})
        with self.assertLogs("src.api.fable_client", "INFO"):
            self.assertIsNone(self.client.resolve_book("Example Book", "Example Author"))


class TestListsAndProgress(FableClientTestCase):
    def setUp(self):
        super().setUp()
        self.routes[("GET", "/api/v2/users/42/book_lists")] = _resp(json=LISTS)
        self.routes[("POST", "/api/v2/users/42/book_lists/book")] = _resp(201, json={})
        self.routes[("POST", f"/api/books/{BOOK_ID}/reading_progress")] = _resp(201, json={})

    def _posts(self):
        return [(path, body) for method, path, body in self.calls if method == "POST"]

    def test_moving_a_book_uses_the_one_list_and_leaves_the_others(self):
        self.routes[("GET", f"/api/books/{BOOK_ID}")] = _resp(json={"response": _book(status="want_to_read")})
        self.assertTrue(self.client.set_shelf(BOOK_ID, "current_reading"))
        (path, body), = self._posts()
        self.assertEqual(body["book_list_ids"], ["list-current"])
        self.assertEqual(sorted(body["exclude_from"]), ["list-dnf", "list-finished", "list-want"])

    def test_no_write_when_the_book_is_already_on_that_list(self):
        self.routes[("GET", f"/api/books/{BOOK_ID}")] = _resp(json={"response": _book(status="finished")})
        self.assertTrue(self.client.set_shelf(BOOK_ID, "finished"))
        self.assertEqual(self._posts(), [])

    def test_already_on_that_list_reply_counts_as_moved(self):
        # Fable answers 409 when its own view already has the book on that list.
        self.routes[("GET", f"/api/books/{BOOK_ID}")] = _resp(json={"response": _book(status="want_to_read")})
        self.routes[("POST", "/api/v2/users/42/book_lists/book")] = _resp(409)
        self.assertTrue(self.client.set_shelf(BOOK_ID, "current_reading"))
        self.assertEqual(self.client.get_shelf(BOOK_ID), (True, "current_reading"))

    def test_list_is_cached_after_a_move(self):
        self.routes[("GET", f"/api/books/{BOOK_ID}")] = _resp(json={"response": _book()})
        self.client.set_shelf(BOOK_ID, "want_to_read")
        self.assertEqual(self.client.get_shelf(BOOK_ID), (True, "want_to_read"))

    def test_unreadable_book_is_not_moved(self):
        self.assertFalse(self.client.set_shelf(BOOK_ID, "finished"))
        self.assertEqual(self._posts(), [])

    def test_progress_is_a_whole_percent(self):
        self.assertTrue(self.client.update_progress(BOOK_ID, 0.426))
        (path, body), = self._posts()
        self.assertEqual((body["current_percentage"], body["selected_mode"]), (43, "percentage"))

    def test_library_reads_every_list_page_and_caches(self):
        pages = {
            "list-want": [{"results": [{"book": _book(OTHER_ID)}], "next": "more"}, {"results": [], "next": None}],
            "list-current": [{"results": [{"book": _book()}], "next": None}],
        }

        def route(method, url, json=None, **kwargs):
            path = url.replace(fable_client.FABLE_API, "")
            self.calls.append((method, path, json))
            if path.startswith("/api/settings/profile/"):
                return _resp(json={"id": 42})
            if path.startswith("/api/v2/users/42/book_lists?"):
                return _resp(json=LISTS)
            for list_id, rows in pages.items():
                if f"/book_lists/{list_id}/books" in path:
                    return _resp(json=rows[int(path.rsplit("offset=", 1)[1]) // 100])
            return _resp(json={"results": [], "next": None})
        self.api.side_effect = route
        books = {b["book_id"]: b["shelf"] for b in self.client.library()}
        self.assertEqual(books, {OTHER_ID: "want_to_read", BOOK_ID: "current_reading"})
        calls = len(self.calls)
        self.client.library()
        self.assertEqual(len(self.calls), calls)  # cached
        self.assertEqual(self.client.get_shelf(BOOK_ID), (True, "current_reading"))

    def test_failed_progress_post_reports_failure(self):
        self.routes[("POST", f"/api/books/{BOOK_ID}/reading_progress")] = _resp(500)
        with self.assertLogs("src.api.fable_client", "WARNING"):
            self.assertFalse(self.client.update_progress(BOOK_ID, 0.5))


if __name__ == "__main__":
    unittest.main()
