from types import SimpleNamespace

from flask import Flask

from src.api import kosync_server
from src.api import reading_position_preview_routes as routes
from src.utils.user_context import reset_current_user_id, set_current_user_id


class FakeDatabase:
    def __init__(self, *, linked=True):
        self.linked = linked
        self.state_calls = []
        self.stats_calls = []

    def get_book(self, abs_id):
        if abs_id == "missing":
            return None
        return SimpleNamespace(abs_id=abs_id)

    def is_user_linked(self, user_id, abs_id):
        return self.linked and user_id == 7 and abs_id == "book-1"

    def get_states_for_book(self, abs_id, user_id=None):
        self.state_calls.append((abs_id, user_id))
        return [SimpleNamespace(client_name="kosync")]

    def get_reading_stats(self, abs_id, user_id=None):
        # The real DatabaseService.get_reading_stats must return 'last_leader' as well (see tests/test_reading_position_preview_last_leader.py)
        self.stats_calls.append((abs_id, user_id))
        return {"last_leader": "KoSync:reader"}


class FakeManager:
    ebook_parser = object()
    alignment_service = object()


def _call_with_user(app, user_id, abs_id):
    token = set_current_user_id(user_id)
    try:
        with app.test_request_context():
            return routes.api_book_position_preview(abs_id)
    finally:
        reset_current_user_id(token)


def test_preview_api_is_user_scoped_and_private_no_store(monkeypatch):
    app = Flask(__name__)
    database = FakeDatabase()
    monkeypatch.setattr(kosync_server, "_database_service", database)
    monkeypatch.setattr(kosync_server, "_manager", FakeManager())

    captured = {}

    def fake_preview(**kwargs):
        captured.update(kwargs)
        return {
            "status": "exact",
            "source": "KoSync",
            "confidence": "Exact · XPath",
            "percentage": 42.0,
            "before": "before",
            "after": "after",
            "message": "",
        }

    monkeypatch.setattr(routes, "build_reading_position_preview", fake_preview)

    response = _call_with_user(app, 7, "book-1")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.get_json()["confidence"] == "Exact · XPath"
    assert database.state_calls == [("book-1", 7)]
    assert database.stats_calls == [("book-1", 7)]
    assert captured["last_leader"] == "KoSync:reader"


def test_preview_api_rejects_book_not_linked_to_current_user(monkeypatch):
    app = Flask(__name__)
    database = FakeDatabase(linked=False)
    monkeypatch.setattr(kosync_server, "_database_service", database)
    monkeypatch.setattr(kosync_server, "_manager", FakeManager())

    response, status = _call_with_user(app, 7, "book-1")

    assert status == 403
    assert response.get_json()["error"] == "book not available for this user"
    assert database.state_calls == []
    assert database.stats_calls == []


def test_preview_api_requires_ambient_authenticated_user(monkeypatch):
    app = Flask(__name__)
    monkeypatch.setattr(kosync_server, "_database_service", FakeDatabase())
    monkeypatch.setattr(kosync_server, "_manager", FakeManager())

    response, status = _call_with_user(app, None, "book-1")

    assert status == 401
    assert response.get_json()["error"] == "authentication required"
