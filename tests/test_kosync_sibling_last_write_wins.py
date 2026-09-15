import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.api import kosync_server
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

USER_ID = 7
OTHER_USER_ID = 8
CANONICAL = "a" * 32
SIBLING_A = "b" * 32
SIBLING_B = "c" * 32


class FakeDatabase:
    def __init__(self, state, rows):
        self.state = state
        self.rows = rows
        self.saved_state = None

    def get_states_for_book(self, _abs_id):
        return [self.state] if self.state is not None else []

    def get_state(self, _abs_id, _client_name, *, user_id):
        return self.state if getattr(self.state, "user_id", user_id) == user_id else None

    def get_user_kosync_progress_for_book(self, _abs_id, user_id):
        return [row for row in self.rows if row.user_id == user_id]

    def get_kosync_documents_for_book(self, _abs_id):
        return self.rows

    def save_state(self, state):
        self.saved_state = state
        self.state = state
        return state


class FakeKoSyncApi:
    def is_configured(self):
        return True

    def get_progress_with_metadata(self, _doc_id):
        return 0.5, "/body/DocFragment[1]/p.0", {}


def make_book(*, filename="book.epub"):
    return SimpleNamespace(
        abs_id="book-1",
        abs_title="Sibling Test",
        kosync_doc_id=CANONICAL,
        sync_mode="ebook",
        ebook_filename=filename,
        original_ebook_filename=filename,
    )


def make_state(
    pct,
    progress,
    *,
    authoritative_at=None,
    authoritative_hash=None,
    authoritative_pct=None,
    last_updated=None,
    locator=None,
    user_id=USER_ID,
):
    metadata = dict(locator or {})
    if authoritative_at is not None:
        metadata["kosync_authoritative_put_at"] = authoritative_at
        metadata["kosync_authoritative_put_hash"] = authoritative_hash or SIBLING_B
        metadata["kosync_authoritative_put_pct"] = (
            pct if authoritative_pct is None else authoritative_pct
        )
    return SimpleNamespace(
        client_name="kosync",
        percentage=pct,
        xpath=progress,
        cfi="",
        timestamp=int(authoritative_at) if authoritative_at is not None else None,
        last_updated=(last_updated if last_updated is not None else time.time()),
        locator_json=json.dumps(metadata) if metadata else None,
        user_id=user_id,
    )


def make_row(
    document_hash,
    pct,
    progress,
    timestamp,
    *,
    user_id=USER_ID,
    device="reader",
    device_id=None,
):
    naive_utc = datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
    return SimpleNamespace(
        document_hash=document_hash,
        percentage=pct,
        progress=progress,
        timestamp=naive_utc,
        device=device,
        device_id=device_id or f"device-{document_hash[0]}",
        user_id=user_id,
    )


@pytest.fixture(autouse=True)
def clean_recent_puts(monkeypatch):
    with kosync_server._kosync_recent_external_puts_lock:
        kosync_server._kosync_recent_external_puts.clear()
    monkeypatch.setenv("KOSYNC_XPATH_ORDER_ENABLED", "false")
    monkeypatch.setenv("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS", "600")
    monkeypatch.setenv("KOSYNC_SIBLING_LAST_WRITE_WINS", "true")
    monkeypatch.setenv("SYNC_TRUST_CORROBORATED_REWIND", "true")
    yield
    with kosync_server._kosync_recent_external_puts_lock:
        kosync_server._kosync_recent_external_puts.clear()


def respond(db, *, user_id=USER_ID, book=None):
    app = Flask(__name__)
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        with app.test_request_context("/"):
            g.kosync_user_id = user_id
            response, status = kosync_server._respond_from_book_states(
                CANONICAL, book or make_book()
            )
            return response.get_json(), status
    finally:
        kosync_server._database_service = old_db


def test_confirmed_lower_sibling_movement_beats_older_higher_sibling():
    now = time.time()
    db = FakeDatabase(make_state(
        0.9137, "page-42", authoritative_at=now,
        authoritative_hash=SIBLING_B, authoritative_pct=0.9137,
    ), [
        make_row(SIBLING_A, 1.0, "page-58", now - 30.0),
        make_row(SIBLING_B, 0.9137, "page-42", now),
    ])
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.9137)
    assert data["progress"] == "page-42"


def test_newer_synced_state_overrides_old_confirmed_put_even_if_metadata_lingers():
    now = time.time()
    put_at = now - 10.0
    db = FakeDatabase(make_state(
        0.40, "synced-40", authoritative_at=put_at,
        authoritative_hash=SIBLING_A, authoritative_pct=0.09,
        last_updated=now,
    ), [make_row(SIBLING_A, 0.09, "reader-9", put_at)])
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.40)
    assert data["progress"] == "synced-40"


def test_confirmed_put_expires_with_recent_put_ttl(monkeypatch):
    cutoff = time.time() - 5.0
    db = FakeDatabase(make_state(
        0.9137, "page-42", authoritative_at=cutoff,
        authoritative_hash=SIBLING_B, authoritative_pct=0.9137,
    ), [
        make_row(SIBLING_A, 1.0, "page-58", cutoff - 30.0),
        make_row(SIBLING_B, 0.9137, "page-42", cutoff),
    ])
    monkeypatch.setenv("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS", "1")
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(1.0)
    assert data["progress"] == "page-58"


def test_feature_flag_restores_furthest_wins(monkeypatch):
    now = time.time()
    db = FakeDatabase(make_state(
        0.50, "reader-50", authoritative_at=now,
        authoritative_hash=SIBLING_B, authoritative_pct=0.50,
    ), [
        make_row(SIBLING_A, 0.90, "reader-90", now - 30.0),
        make_row(SIBLING_B, 0.50, "reader-50", now),
    ])
    monkeypatch.setenv("KOSYNC_SIBLING_LAST_WRITE_WINS", "false")
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.90)


def test_global_corroboration_setting_disables_sibling_preference(monkeypatch):
    now = time.time()
    db = FakeDatabase(make_state(
        0.50, "reader-50", authoritative_at=now,
        authoritative_hash=SIBLING_B, authoritative_pct=0.50,
    ), [
        make_row(SIBLING_A, 0.90, "reader-90", now - 30.0),
        make_row(SIBLING_B, 0.50, "reader-50", now),
    ])
    monkeypatch.setenv("SYNC_TRUST_CORROBORATED_REWIND", "false")
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.90)


def test_stale_lower_sibling_without_confirmed_movement_is_pulled_forward():
    now = time.time()
    db = FakeDatabase(make_state(0.30, "stale-30"), [
        make_row(SIBLING_A, 0.80, "reader-80", now - 30.0),
        make_row(SIBLING_B, 0.30, "stale-30", now),
    ])
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.80)


def test_confirmed_reader_movement_requires_same_device_and_real_advance():
    previous = make_row(SIBLING_B, 0.30, "old", time.time() - 10, device_id="reader-b")
    book = make_book()
    assert not kosync_server._is_confirmed_reader_movement(previous, 0.30, "old", "reader-b", book)
    assert not kosync_server._is_confirmed_reader_movement(previous, 0.31, "new", "other-reader", book)
    assert kosync_server._is_confirmed_reader_movement(previous, 0.31, "new", "reader-b", book)
    assert not kosync_server._is_confirmed_reader_movement(None, 0.31, "new", "reader-b", book)


def test_cbz_page_advance_can_confirm_movement_when_percentage_is_unchanged():
    previous = make_row(SIBLING_B, 0.50, "41", time.time() - 10, device_id="reader-b")
    assert kosync_server._is_confirmed_reader_movement(
        previous, 0.50, "42", "reader-b", make_book(filename="book.cbz")
    )


def test_higher_sibling_must_be_older_than_confirmed_put():
    now = time.time()
    rows = [
        make_row(SIBLING_A, 0.80, "ahead", now + 2.0),
        make_row(SIBLING_B, 0.31, "current", now),
    ]
    assert not kosync_server._has_older_higher_sibling(rows, SIBLING_B, 0.31, now)
    rows[0] = make_row(SIBLING_A, 0.80, "ahead", now - 10.0)
    assert kosync_server._has_older_higher_sibling(rows, SIBLING_B, 0.31, now)


def test_authority_is_bound_to_exact_hash_and_rejects_internal_row():
    now = time.time()
    state = make_state(
        0.50, "reader-50", authoritative_at=now,
        authoritative_hash=SIBLING_B, authoritative_pct=0.50,
    )
    wrong_hash = make_row(SIBLING_A, 0.50, "wrong", now + 1.0)
    internal = make_row(
        SIBLING_B, 0.50, "internal", now,
        device="abs-sync-bot", device_id="abs-sync-bot",
    )
    assert kosync_server._latest_authoritative_kosync_put_document(
        [wrong_hash, internal], state
    ) is None


def test_confirmed_put_is_user_scoped():
    now = time.time()
    db = FakeDatabase(make_state(
        0.80, "user-a-current", authoritative_at=now,
        authoritative_hash=SIBLING_A, authoritative_pct=0.80,
    ), [
        make_row(SIBLING_A, 0.80, "user-a-current", now, user_id=USER_ID),
        make_row(SIBLING_B, 0.30, "other-user-row", now, user_id=OTHER_USER_ID),
    ])
    data, status = respond(db, user_id=USER_ID)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.80)


def test_non_cbz_put_does_not_inherit_stale_locator_metadata():
    cutoff = datetime(2026, 7, 15, 12, 34, 56)
    previous = make_state(
        0.50, "/old",
        locator={
            "xpath": "/old",
            "cfi": "epubcfi(/old)",
            "href": "old.xhtml",
            "future_metadata": "do-not-copy-position-metadata",
        },
    )
    db = FakeDatabase(previous, [])
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        kosync_server._record_user_kosync_state(
            make_book(), 0.55, "/new", cutoff, USER_ID,
            authoritative_document_hash=SIBLING_B,
        )
    finally:
        kosync_server._database_service = old_db
    locator = json.loads(db.saved_state.locator_json)
    assert locator == {
        "kosync_authoritative_put_at": cutoff.replace(tzinfo=timezone.utc).timestamp(),
        "kosync_authoritative_put_hash": SIBLING_B,
        "kosync_authoritative_put_pct": 0.55,
    }
    assert db.saved_state.locator_source is None
    assert db.saved_state.xpath == "/new"


def test_external_cbz_put_preserves_page_and_approved_rewind_only():
    cutoff = datetime(2026, 7, 15, 12, 34, 56)
    previous = make_state(
        0.50, "41",
        locator={"page": 41, "kosync_approved_rewind_at": 1234.5, "xpath": "/stale"},
    )
    db = FakeDatabase(previous, [])
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        kosync_server._record_user_kosync_state(
            make_book(filename="book.cbz"), 0.55, "42", cutoff, USER_ID,
            authoritative_document_hash=SIBLING_B,
        )
    finally:
        kosync_server._database_service = old_db
    locator = json.loads(db.saved_state.locator_json)
    assert locator == {
        "kosync_approved_rewind_at": 1234.5,
        "kosync_authoritative_put_at": cutoff.replace(tzinfo=timezone.utc).timestamp(),
        "kosync_authoritative_put_hash": SIBLING_B,
        "kosync_authoritative_put_pct": 0.55,
        "page": 41,
    }


def test_kosync_client_does_not_carry_temporary_authority_through_sync_state():
    cutoff = time.time() - 3.0
    prev_state = make_state(
        0.5, "/body/DocFragment[1]/p.0", authoritative_at=cutoff,
        authoritative_hash=SIBLING_B, authoritative_pct=0.5,
    )
    client = KoSyncSyncClient(FakeKoSyncApi(), SimpleNamespace())
    service_state = client.get_service_state(make_book(), prev_state)
    assert "kosync_authoritative_put_at" not in service_state.current
    assert "kosync_authoritative_put_hash" not in service_state.current


def test_setting_is_registered_defaulted_boolean_and_rendered():
    assert "KOSYNC_SIBLING_LAST_WRITE_WINS" in ALL_SETTINGS
    assert DEFAULT_CONFIG["KOSYNC_SIBLING_LAST_WRITE_WINS"] == "true"
    root = Path(__file__).parent.parent
    template = (root / "templates" / "settings.html").read_text(encoding="utf-8")
    assert 'name="KOSYNC_SIBLING_LAST_WRITE_WINS"' in template
    assert "Prefer Confirmed Recent Reader Movement" in template
    web_source = (root / "src" / "web_server.py").read_text(encoding="utf-8")
    bool_block = web_source.split("bool_keys = [", 1)[1].split("]", 1)[0]
    assert "'KOSYNC_SIBLING_LAST_WRITE_WINS'" in bool_block
