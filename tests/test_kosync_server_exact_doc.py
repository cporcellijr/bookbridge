from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from flask import Flask

from src.api import kosync_server


def test_respond_from_book_states_prefers_exact_document_state(monkeypatch):
    app = Flask(__name__)
    db = MagicMock()
    exact_doc = SimpleNamespace(
        document_hash="doc-exact",
        percentage=0.42,
        progress="/body/DocFragment[1]/body/p[1]/text().0",
        device="device-a",
        device_id="dev-a",
        timestamp=datetime.fromtimestamp(1234, UTC),
    )
    sibling_doc = SimpleNamespace(
        document_hash="doc-sibling",
        percentage=0.9,
        progress="/body/DocFragment[9]/body/p[9]/text().0",
        device="device-b",
        device_id="dev-b",
        timestamp=datetime.fromtimestamp(5678, UTC),
    )
    db.get_states_for_book.return_value = []
    db.get_kosync_document.return_value = exact_doc
    db.get_kosync_documents_for_book.return_value = [exact_doc, sibling_doc]
    monkeypatch.setattr(kosync_server, "_database_service", db)

    with app.app_context():
        response, status_code = kosync_server._respond_from_book_states(
            "doc-exact",
            SimpleNamespace(abs_id="abs-1", abs_title="Test Book"),
        )

    payload = response.get_json()
    assert status_code == 200
    assert payload["document"] == "doc-exact"
    assert payload["percentage"] == 0.42
    assert payload["progress"] == "/body/DocFragment[1]/body/p[1]/text().0"
