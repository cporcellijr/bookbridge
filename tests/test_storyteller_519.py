"""Regression coverage for Storyteller OPDS hashes and linked-book reconciliation (#519)."""

import hashlib
import os
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import event

from src.db.database_service import DatabaseService
from src.db.models import Book, KosyncDocument

import pytest

from src.api.storyteller_api import StorytellerAPIClient
from src.services.storyteller_library_watch import register_hashes, scan_linked
from src.utils.ebook_utils import EbookParser


class FakeResponse:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=1024):
        for idx in range(0, len(self.content), chunk_size):
            yield self.content[idx:idx + chunk_size]


def hash_client(content, ignore_range=False):
    client = StorytellerAPIClient()
    client.base_url = "http://storyteller.test"
    client._get_fresh_token = Mock(return_value="token")

    responses = []

    def response(*args):
        value = FakeResponse(*args)
        responses.append(value)
        return value

    client.responses = responses

    def get(url, *, headers, params, timeout, stream):
        assert params["format"] in {"ebook", "readaloud"}
        assert stream
        if ignore_range:
            return response(200, content)
        start = int(headers["Range"].split("=")[1].split("-")[0])
        if start >= len(content):
            return response(416)
        end = min(len(content), start + 1024)
        return response(206, content[start:end],
                            {"Content-Range": f"bytes {start}-{end - 1}/{len(content)}"})

    client.session.get = Mock(side_effect=get)
    return client


@pytest.mark.parametrize("size", [500, 1580, 12345, 95000])
def test_range_hash_matches_full_epub_hash(size):
    contents = bytes(i % 251 for i in range(size))
    client = hash_client(contents)
    expected = EbookParser._compute_koreader_hash_from_bytes(None, contents)
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        assert client.get_kosync_content_hash("a-uuid", "ebook") == expected
    assert client.session.get.call_count <= 12
    assert client.responses and all(response.closed for response in client.responses)


def test_200_fallback_hashes_streamed_full_body():
    contents = hashlib.shake_256(b"storyteller-519-stream").digest(350000)
    client = hash_client(contents, ignore_range=True)
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        assert client.get_kosync_content_hash("a-uuid", "readaloud") == (
            EbookParser._compute_koreader_hash_from_bytes(None, contents))
    client.session.get.assert_called_once()
    assert client.responses[0].closed


def test_filename_method_does_not_call_storyteller():
    client = hash_client(b"book")
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "filename"}):
        assert client.get_kosync_content_hash("uuid", "ebook") is None
    client.session.get.assert_not_called()


def setup_registration(artifact=True, owner=None):
    book = SimpleNamespace(
        abs_id="ebook-1", storyteller_uuid="abcd-1234",
        ebook_filename="storyteller_abcd-1234.epub",
        original_ebook_filename=None if artifact else "plain.epub",
    )
    state = {}
    db = Mock()
    db.get_json_setting.side_effect = lambda key, default=None: state.get(key, default)
    db.set_json_setting.side_effect = lambda key, value: state.__setitem__(key, dict(value))
    db.get_kosync_document.side_effect = (
        lambda value: SimpleNamespace(linked_abs_id=owner) if value == "b" * 32 and owner else None
    )
    client = Mock()
    client.is_configured.return_value = True
    client.get_kosync_content_hash.side_effect = (
        lambda uuid, fmt: {"ebook": "a" * 32, "readaloud": "b" * 32}[fmt]
    )
    details = {"readaloud": {"status": "ALIGNED", "missing": False, "fingerprint": "v1"}}
    return db, client, book, details, db.adopt_kosync_document_if_unowned, state


def test_registers_plain_and_readaloud_for_artifact_only():
    for artifact, expected in ((True, 2), (False, 1)):
        db, client, book, details, adopt, _ = setup_registration(artifact=artifact)
        with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
            assert register_hashes(db, client, book, details, user_id=1)
        assert adopt.call_count == expected
        adopt.assert_any_call("a" * 32, book.abs_id, expected_storyteller_uuid="abcd-1234")
        if not artifact:
            client.get_kosync_content_hash.assert_called_once_with("abcd-1234", "ebook")


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_JOURNAL_MODE", "DELETE")
    db = DatabaseService(str(tmp_path / "storyteller519.db"))
    try:
        yield db
    finally:
        db.db_manager.close()


def test_conflicting_hash_is_never_repointed(real_db, monkeypatch):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    _, client, _, details, _, _ = setup_registration()
    book = real_db.save_book(Book(abs_id="ebook-1", storyteller_uuid="abcd-1234",
                                 ebook_filename="storyteller_abcd-1234.epub"))
    real_db.save_book(Book(abs_id="other-book"))
    with real_db.get_session() as session:
        session.add(KosyncDocument(document_hash="b" * 32, linked_abs_id="other-book",
                                   percentage=0.4, progress="keep-me"))
    assert register_hashes(real_db, client, book, details, user_id=3)
    assert real_db.get_kosync_document("a" * 32).linked_abs_id == book.abs_id
    other = real_db.get_kosync_document("b" * 32)
    assert (other.linked_abs_id, other.percentage, other.progress) == ("other-book", Decimal("0.4"), "keep-me")


def test_unchanged_fingerprint_skips_rehash_and_change_retains_old_hash():
    db, client, book, details, adopt, state = setup_registration()
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        register_hashes(db, client, book, details, user_id=1)
        client.get_kosync_content_hash.reset_mock()
        register_hashes(db, client, book, details, user_id=1)
        client.get_kosync_content_hash.assert_not_called()
        details["readaloud"]["fingerprint"] = "v2"
        client.get_kosync_content_hash.side_effect = (
            lambda uuid, fmt: {"ebook": "c" * 32, "readaloud": "d" * 32}[fmt]
        )
        register_hashes(db, client, book, details, user_id=1)
    assert ("b" * 32, "ebook-1") in [c.args for c in adopt.call_args_list]
    assert ("d" * 32, "ebook-1") in [c.args for c in adopt.call_args_list]
    assert state["STORYTELLER_519_HASHES:1"]["abcd-1234"]["fingerprint"] == "v2"


def test_reconciler_only_handles_passed_user_books():
    db, client, book, details, adopt, _ = setup_registration()
    other = SimpleNamespace(abs_id="other", storyteller_uuid="other-uuid")
    client.list_library_books.return_value = [
        {"uuid": "abcd-1234", **details},
        {"uuid": "other-uuid", **details},
    ]
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        scan_linked(db, client, [book], 17)
    client.get_kosync_content_hash.assert_any_call("abcd-1234", "ebook")
    assert all(call.args[0] == "abcd-1234" for call in client.get_kosync_content_hash.call_args_list)
    assert "STORYTELLER_519_HASHES:17" in _


@pytest.mark.parametrize("setting", ["true", "on"])
def test_auto_link_creates_and_claims_aligned_storyteller_book(setting):
    from src import web_server as ws

    client = Mock()
    client.get_kosync_content_hash.return_value = "a" * 32
    library = [{"uuid": "abcd-1234", "title": "Solo Book",
                "authors": [{"name": "Writer"}],
                "readaloud": {"status": "ALIGNED", "missing": False}}]
    saved = SimpleNamespace(abs_id="ebook-1", storyteller_uuid="abcd-1234")
    db = Mock()
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_document.return_value = None
    with patch.object(ws, "database_service", db), \
         patch.object(ws, "user_setting", return_value=setting), \
         patch.object(ws, "_create_ebook_only_mapping_from_queue_item",
                      return_value=saved) as create, \
         patch.object(ws, "_claim_book_for_user_id") as claim:
        mapped = []
        assert ws._storyteller_auto_link_candidates(7, client, library, mapped, set()) == 1
    create.assert_called_once()
    assert create.call_args.args[0]["storyteller_uuid"] == "abcd-1234"
    claim.assert_called_with(7, "ebook-1")
    assert mapped == [saved]


def test_auto_link_disabled_does_not_touch_files():
    from src import web_server as ws

    client = Mock()
    item = {"uuid": "abcd-1234", "readaloud": {"status": "ALIGNED"}}
    with patch.object(ws, "user_setting", return_value="false"):
        assert ws._storyteller_auto_link_candidates(3, client, [item], [], set()) == 0
    client.get_kosync_content_hash.assert_not_called()


@pytest.mark.parametrize("readaloud", [
    {"status": "PROCESSING", "missing": False},
    {"status": "ALIGNED", "missing": True},
])
def test_auto_link_waits_for_complete_alignment(readaloud):
    from src import web_server as ws

    client = Mock()
    item = {"uuid": "abcd-1234", "readaloud": readaloud}
    with patch.object(ws, "user_setting", return_value="on"):
        assert ws._storyteller_auto_link_candidates(3, client, [item], [], set()) == 0
    client.get_kosync_content_hash.assert_not_called()


def test_auto_link_skips_uuid_already_mapped():
    from src import web_server as ws

    client = Mock()
    book = SimpleNamespace(
        storyteller_uuid="abcd-1234", abs_id="ebook-1",
        ebook_filename="storyteller_abcd-1234.epub", kosync_doc_id="a" * 32,
    )
    item = {"uuid": "abcd-1234", "readaloud": {"status": "ALIGNED"}}
    with patch.object(ws, "user_setting", return_value="true"), \
         patch.object(ws, "_claim_book_for_user_id") as claim:
        assert ws._storyteller_auto_link_candidates(
            3, client, [item], [book], {"ebook-1"}) == 0
    claim.assert_not_called()
    client.get_kosync_content_hash.assert_not_called()


def test_auto_link_hash_match_attaches_without_replacing_primary():
    from src import web_server as ws

    existing = SimpleNamespace(
        abs_id="abs-1", abs_title="Original", storyteller_uuid=None,
        ebook_filename="original.epub", kosync_doc_id="a" * 32,
    )
    db = Mock()
    db.get_book_by_kosync_id.return_value = existing
    client = Mock()
    client.get_kosync_content_hash.return_value = "a" * 32
    item = {"uuid": "abcd-1234", "readaloud": {"status": "ALIGNED", "missing": False}}
    with patch.object(ws, "database_service", db), \
         patch.object(ws, "user_setting", return_value="on"), \
         patch.object(ws, "_existing_mapping_access", return_value="modify"), \
         patch.object(ws, "_upsert_storyteller_mapping",
                      return_value=(existing, None, None)) as attach, \
         patch.object(ws, "_claim_book_for_user_id") as claim:
        assert ws._storyteller_auto_link_candidates(
            3, client, [item], [existing], {"abs-1"}) == 1
    assert attach.call_args.kwargs["preserve_primary_kosync"] is True
    assert attach.call_args.kwargs["existing_book"] is existing
    claim.assert_called_with(3, "abs-1")


def test_auto_link_title_collision_is_left_for_manual_add():
    from src import web_server as ws

    existing = SimpleNamespace(abs_id="bo-1", abs_title="Same Book", storyteller_uuid=None)
    db = Mock()
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_document.return_value = None
    client = Mock()
    client.get_kosync_content_hash.return_value = "a" * 32
    item = {"uuid": "abcd-1234", "title": "Same Book",
            "authors": [{"name": "Writer"}],
            "readaloud": {"status": "ALIGNED", "missing": False}}
    with patch.object(ws, "database_service", db), \
         patch.object(ws, "user_setting", return_value="on"), \
         patch.object(ws, "_create_ebook_only_mapping_from_queue_item") as create:
        assert ws._storyteller_auto_link_candidates(
            3, client, [item], [existing], {"bo-1"}) == 0
    create.assert_not_called()


def test_auto_link_remembers_declined_books_so_newer_ones_still_link():
    from src import web_server as ws

    store = {}
    db = Mock()
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_document.return_value = None
    db.get_json_setting.side_effect = lambda key, default=None: store.get(key, default)
    db.set_json_setting.side_effect = lambda key, value: store.__setitem__(key, value)
    client = Mock()
    client.get_kosync_content_hash.return_value = "a" * 32
    aligned = {"status": "ALIGNED", "missing": False, "fingerprint": "fp-1"}
    mapped = [SimpleNamespace(abs_id=f"bo-{n}", abs_title=f"Taken {n}", storyteller_uuid=None)
              for n in range(7)]
    library = [{"uuid": f"dup-{n}", "title": f"Taken {n}", "readaloud": aligned} for n in range(7)]
    library.append({"uuid": "fresh", "title": "Fresh Book", "readaloud": aligned})
    saved = SimpleNamespace(abs_id="ebook-new", storyteller_uuid="fresh")
    with patch.object(ws, "database_service", db), \
         patch.object(ws, "user_setting", return_value="on"), \
         patch.object(ws, "_create_ebook_only_mapping_from_queue_item", return_value=saved) as create, \
         patch.object(ws, "_claim_book_for_user_id"):
        assert ws._storyteller_auto_link_candidates(3, client, library, list(mapped), set()) == 0
        assert ws._storyteller_auto_link_candidates(3, client, library, list(mapped), set()) == 1
    create.assert_called_once()
    assert create.call_args.args[0]["storyteller_uuid"] == "fresh"


def test_auto_link_foreign_uuid_uses_existing_join_policy():
    from src import web_server as ws

    existing = SimpleNamespace(
        abs_id="ebook-other", storyteller_uuid="abcd-1234",
        ebook_filename="plain.epub", kosync_doc_id="a" * 32,
    )
    item = {"uuid": "abcd-1234", "readaloud": {"status": "ALIGNED", "missing": False}}
    with patch.object(ws, "user_setting", return_value="on"), \
         patch.object(ws, "_existing_mapping_access", return_value="join") as access, \
         patch.object(ws, "_claim_book_for_user_id") as claim:
        assert ws._storyteller_auto_link_candidates(
            7, Mock(), [item], [existing], set()) == 0
    access.assert_called_once()
    claim.assert_called_once_with(7, "ebook-other")


def test_periodic_pass_filters_foreign_books_before_hash_registration():
    from src import web_server as ws

    client = Mock()
    client.is_configured.return_value = True
    client.list_library_books.return_value = []
    mine = SimpleNamespace(abs_id="mine", storyteller_uuid="mine-uuid")
    other = SimpleNamespace(abs_id="other", storyteller_uuid="other-uuid")
    db = Mock()
    db.get_linked_abs_ids.return_value = ["mine"]
    db.get_all_books.return_value = [mine, other]
    with patch.object(ws, "database_service", db), \
         patch.object(ws, "uc", return_value=SimpleNamespace(storyteller_client=client)), \
         patch.object(ws, "_storyteller_auto_link_candidates", return_value=0), \
         patch("src.services.storyteller_library_watch.scan_linked") as scan:
        ws._storyteller_user_library_pass(7)
    assert scan.call_args.args[2] == [mine]
    assert scan.call_args.args[3] == 7


def test_reconciliation_bounds_changed_hashes_per_pass():
    db = Mock()
    state = {}
    db.get_json_setting.side_effect = lambda key, default=None: state.get(key, default)
    db.set_json_setting.side_effect = lambda key, value: state.__setitem__(key, dict(value))
    db.get_kosync_document.return_value = None
    client = Mock()
    client.is_configured.return_value = True
    client.get_kosync_content_hash.side_effect = (
        lambda uuid, fmt: "a" * 32 if fmt == "ebook" else "b" * 32
    )
    books = [
        SimpleNamespace(abs_id=f"ebook-{i}", storyteller_uuid=f"abcd-{i:04d}",
                        ebook_filename=f"storyteller_abcd-{i:04d}.epub",
                        original_ebook_filename=None)
        for i in range(8)
    ]
    client.list_library_books.return_value = [
        {"uuid": book.storyteller_uuid,
         "readaloud": {"status": "ALIGNED", "missing": False, "fingerprint": "v1"}}
        for book in books
    ]
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        scan_linked(db, client, books, user_id=12)
    assert client.get_kosync_content_hash.call_count == 8  # 4 books x both editions
    assert state["STORYTELLER_519_HASHES:12"]["_cursor"] == 4


def test_per_user_auto_link_setting_registry():
    from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG
    from src.utils.user_config import PER_USER_CREDENTIAL_KEYS, PER_USER_FIELD_GROUPS

    assert "STORYTELLER_AUTO_LINK" in ALL_SETTINGS
    assert DEFAULT_CONFIG["STORYTELLER_AUTO_LINK"] == "false"
    assert "STORYTELLER_AUTO_LINK" in PER_USER_CREDENTIAL_KEYS
    fields = [field for group, values in PER_USER_FIELD_GROUPS
              if group == "Storyteller" for field in values]
    assert ("STORYTELLER_AUTO_LINK",
            "Automatically link aligned Storyteller books", "bool") in fields


def test_hourly_storyteller_pass_fails_closed_on_missing_user_bundle():
    from src import web_server as ws

    user = SimpleNamespace(id=7, active=True, is_admin=False)
    db = Mock()
    db.list_users.return_value = [user]
    registry = Mock()
    registry.get_clients.side_effect = RuntimeError("registry unavailable")
    container = Mock()
    container.user_client_registry.return_value = registry
    with patch.object(ws, "database_service", db), \
         patch.object(ws, "container", container), \
         patch.object(ws, "_spawn_user_background") as spawn:
        ws._run_storyteller_library_hourly()
    spawn.assert_not_called()


def test_link_time_storyteller_hash_lookup_never_uses_global_for_user():
    from src import web_server as ws
    from src.utils.user_context import set_current_user_id, reset_current_user_id

    db = Mock()
    db.get_book.return_value = SimpleNamespace(
        abs_id="ebook-7", storyteller_uuid="abcd-1234",
    )
    registry = Mock()
    registry.get_clients.side_effect = RuntimeError("registry unavailable")
    container = Mock()
    container.user_client_registry.return_value = registry
    global_client = Mock()
    token = set_current_user_id(7)
    try:
        with patch.object(ws, "database_service", db), \
             patch.object(ws, "container", container), \
             patch.object(ws, "uc", return_value=SimpleNamespace(storyteller_client=global_client)):
            ws._storyteller_register_file_hashes("abcd-1234", "ebook-7")
    finally:
        reset_current_user_id(token)
    global_client.get_kosync_content_hash.assert_not_called()


@pytest.mark.parametrize("case,expected", [
    ("new", True), ("unlinked", True), ("own", False),
    ("foreign_doc", False), ("foreign_primary", False),
    ("foreign_primary_unlinked", False), ("missing", False), ("stale_uuid", False),
])
def test_atomic_adoption_checks_book_and_both_owners(real_db, case, expected):
    if case != "missing":
        real_db.save_book(Book(abs_id="target", storyteller_uuid="current"))
    real_db.save_book(Book(abs_id="other", kosync_doc_id="hash" if "foreign_primary" in case else None))
    if case in {"unlinked", "own", "foreign_doc", "foreign_primary_unlinked"}:
        owner = {"own": "target", "foreign_doc": "other"}.get(case)
        with real_db.get_session() as session:
            session.add(KosyncDocument(document_hash="hash", linked_abs_id=owner,
                                       progress="keep", percentage=0.6))
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(real_db.db_manager.engine, "before_cursor_execute", capture)
    try:
        result = real_db.adopt_kosync_document_if_unowned(
            "hash", "target", expected_storyteller_uuid="stale" if case == "stale_uuid" else "current")
    finally:
        event.remove(real_db.db_manager.engine, "before_cursor_execute", capture)
    assert result is expected
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("INSERT")
    doc = real_db.get_kosync_document("hash")
    if expected or case == "own":
        assert doc.linked_abs_id == "target"
    elif case == "foreign_doc":
        assert doc.linked_abs_id == "other"
    else:
        assert doc is None or doc.linked_abs_id is None
    if case in {"unlinked", "own", "foreign_doc", "foreign_primary_unlinked"}:
        assert (doc.progress, doc.percentage) == ("keep", Decimal("0.6"))


def test_atomic_adoption_refuses_an_owner_claimed_just_before_insert(real_db):
    real_db.save_book(Book(abs_id="target"))
    real_db.save_book(Book(abs_id="other"))
    interleaved = False
    def claim_first(conn, cursor, statement, parameters, context, many):
        nonlocal interleaved
        if not interleaved and statement.lstrip().upper().startswith("INSERT"):
            interleaved = True
            assert real_db.adopt_kosync_document_if_unowned("hash", "other")
    event.listen(real_db.db_manager.engine, "before_cursor_execute", claim_first)
    try:
        assert not real_db.adopt_kosync_document_if_unowned("hash", "target")
    finally:
        event.remove(real_db.db_manager.engine, "before_cursor_execute", claim_first)
    assert interleaved
    assert real_db.get_kosync_document("hash").linked_abs_id == "other"


@pytest.mark.parametrize("change", ["delete", "remap"])
def test_registration_rechecks_book_after_network_requests(real_db, monkeypatch, change):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    _, client, _, details, _, _ = setup_registration()
    book = real_db.save_book(Book(abs_id="target", storyteller_uuid="abcd-1234",
                                 ebook_filename="storyteller_abcd-1234.epub"))
    def mutate_during_hash(uuid, fmt):
        if fmt == "ebook":
            if change == "delete":
                real_db.delete_book(book.abs_id)
            else:
                current = real_db.get_book(book.abs_id)
                current.storyteller_uuid = "different"
                real_db.update_book_if_exists(current)
        return "a" * 32 if fmt == "ebook" else "b" * 32
    client.get_kosync_content_hash.side_effect = mutate_during_hash
    register_hashes(real_db, client, book, details, user_id=3)
    assert real_db.get_kosync_document("a" * 32) is None
    assert real_db.get_kosync_document("b" * 32) is None


def test_web_adoption_preserves_primary_only_foreign_owner(real_db, monkeypatch):
    from src import web_server as ws
    real_db.save_book(Book(abs_id="target"))
    real_db.save_book(Book(abs_id="other", kosync_doc_id="hash"))
    monkeypatch.setattr(ws, "database_service", real_db)
    ws._adopt_kosync_progress_for_book("target", "hash")
    assert real_db.get_kosync_document("hash") is None


def test_unchanged_registration_and_cursor_do_not_write(real_db, monkeypatch):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    _, client, _, details, _, _ = setup_registration()
    book = real_db.save_book(Book(abs_id="target", storyteller_uuid="abcd-1234",
                                 ebook_filename="storyteller_abcd-1234.epub"))
    library = [{"uuid": book.storyteller_uuid, **details}]
    scan_linked(real_db, client, [book], 3, library=library)
    client.get_kosync_content_hash.reset_mock()
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(real_db.db_manager.engine, "before_cursor_execute", capture)
    try:
        scan_linked(real_db, client, [book], 3, library=library)
    finally:
        event.remove(real_db.db_manager.engine, "before_cursor_execute", capture)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    # Saving an equal JSON value still performs extra settings SELECTs and a transaction.
    assert sum("FROM settings" in s for s in statements) == 3
    client.get_kosync_content_hash.assert_not_called()


def test_rehash_retains_old_rows_in_real_database(real_db, monkeypatch):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    _, client, _, details, _, _ = setup_registration()
    book = real_db.save_book(Book(abs_id="target", storyteller_uuid="abcd-1234",
                                 ebook_filename="storyteller_abcd-1234.epub"))
    register_hashes(real_db, client, book, details, user_id=3)
    details["readaloud"]["fingerprint"] = "v2"
    client.get_kosync_content_hash.side_effect = lambda uuid, fmt: "c" * 32 if fmt == "ebook" else "d" * 32
    register_hashes(real_db, client, book, details, user_id=3)
    assert all(real_db.get_kosync_document(h * 32).linked_abs_id == book.abs_id for h in "abcd")


@pytest.fixture
def auto_link_env(monkeypatch):
    import copy
    from src import web_server as ws
    store = {}
    db = Mock()
    db.get_json_setting.side_effect = lambda key, default=None: copy.deepcopy(store.get(key, default))
    db.set_json_setting.side_effect = lambda key, value: store.__setitem__(key, copy.deepcopy(value))
    db.get_book_by_kosync_id.return_value = None
    db.get_kosync_document.return_value = None
    monkeypatch.setattr(ws, "database_service", db)
    monkeypatch.setattr(ws, "user_setting", lambda *args: "true")
    client = Mock()
    client.get_kosync_content_hash.return_value = "a" * 32
    create = Mock(side_effect=lambda item: SimpleNamespace(
        abs_id="ebook-" + item["storyteller_uuid"], storyteller_uuid=item["storyteller_uuid"],
        abs_title=item.get("abs_title", "")))
    claim = Mock()
    monkeypatch.setattr(ws, "_create_ebook_only_mapping_from_queue_item", create)
    monkeypatch.setattr(ws, "_claim_book_for_user_id", claim)
    return ws, db, client, store, create, claim


def aligned_item(uuid, title=None, fingerprint="fp"):
    return {"uuid": uuid, "title": title or uuid,
            "readaloud": {"status": "ALIGNED", "fingerprint": fingerprint}}


def test_already_mapped_joins_have_a_separate_cap(auto_link_env, monkeypatch):
    ws, db, client, store, create, claim = auto_link_env
    books = [SimpleNamespace(abs_id=f"b{i}", abs_title=f"u{i}", storyteller_uuid=f"u{i}",
                            ebook_filename="plain.epub", kosync_doc_id="a" * 32) for i in range(30)]
    library = [aligned_item(b.storyteller_uuid) for b in books]
    library.extend(aligned_item(f"fresh{i}") for i in range(3))
    monkeypatch.setattr(ws, "_existing_mapping_access", lambda *args, **kwargs: "join")
    claimed = set()
    assert ws._storyteller_auto_link_candidates(7, client, library, books, claimed) == 3
    joined = [c for c in claim.call_args_list if c.args[1].startswith("b")]
    assert len(joined) == 25
    assert create.call_count == 3


@pytest.mark.parametrize("failure", ["hash", "attach", "create", "exception"])
def test_auto_cursor_advances_past_every_failed_attempt(auto_link_env, monkeypatch, failure):
    ws, db, client, store, create, claim = auto_link_env
    library = [aligned_item(f"u{i}") for i in range(7)]
    def content_hash(uuid, fmt):
        index = int(uuid[1:])
        if index < 6:
            if failure == "hash":
                return None
            if failure == "exception":
                raise RuntimeError("offline")
        return f"{index + 1:032x}"
    client.get_kosync_content_hash.side_effect = content_hash
    if failure == "attach":
        db.get_book_by_kosync_id.side_effect = lambda h: (
            SimpleNamespace(abs_id="old", abs_title="Old", storyteller_uuid=None,
                            ebook_filename="plain.epub", kosync_doc_id=h) if int(h, 16) <= 6 else None)
        monkeypatch.setattr(ws, "_existing_mapping_access", lambda *args, **kwargs: "modify")
        monkeypatch.setattr(ws, "_upsert_storyteller_mapping", Mock(return_value=(None, "download failed", 500)))
    if failure == "create":
        create.side_effect = lambda item: (SimpleNamespace(abs_id="fresh", storyteller_uuid="u6", abs_title="Fresh")
                                          if item["storyteller_uuid"] == "u6" else None)
    assert ws._storyteller_auto_link_candidates(7, client, library, [], set()) == 0
    assert store["STORYTELLER_519_HASHES:7"]["_auto_cursor"] == 6
    client.get_kosync_content_hash.reset_mock()
    assert ws._storyteller_auto_link_candidates(7, client, library, [], set()) == 1
    assert client.get_kosync_content_hash.call_args_list[0].args[0] == "u6"


def test_auto_cursor_is_per_reader(auto_link_env):
    ws, db, client, store, create, claim = auto_link_env
    library = [aligned_item(f"u{i}") for i in range(8)]
    client.get_kosync_content_hash.return_value = None
    ws._storyteller_auto_link_candidates(7, client, library, [], set())
    ws._storyteller_auto_link_candidates(8, client, library, [], set())
    calls = [c.args[0] for c in client.get_kosync_content_hash.call_args_list]
    assert calls == [f"u{i}" for i in range(6)] * 2
    assert store["STORYTELLER_519_HASHES:7"]["_auto_cursor"] == 6
    assert store["STORYTELLER_519_HASHES:8"]["_auto_cursor"] == 6


def test_decline_stores_fingerprint_and_timestamp(auto_link_env, monkeypatch):
    ws, db, client, store, create, claim = auto_link_env
    monkeypatch.setattr(ws.time, "time", lambda: 2_000_000_000)
    collision = SimpleNamespace(abs_id="other", abs_title="Same", storyteller_uuid=None)
    ws._storyteller_auto_link_candidates(7, client, [aligned_item("u", "Same")], [collision], set())
    assert store["STORYTELLER_519_HASHES:7"]["_auto_skip"]["u"] == {
        "fingerprint": "fp", "ts": 2_000_000_000}


@pytest.mark.parametrize("age,fingerprint,legacy,expected", [
    (0, "fp", False, 0), (7 * 24 * 60 * 60 - 1, "fp", False, 0),
    (7 * 24 * 60 * 60, "fp", False, 1), (0, "changed", False, 1),
    (0, "fp", True, 1),
])
def test_declines_expire_or_retry_a_new_fingerprint(auto_link_env, monkeypatch, age, fingerprint, legacy, expected):
    ws, db, client, store, create, claim = auto_link_env
    now = 2_000_000_000
    monkeypatch.setattr(ws.time, "time", lambda: now)
    decline = "fp" if legacy else {"fingerprint": "fp", "ts": now - age}
    store["STORYTELLER_519_HASHES:7"] = {"_auto_skip": {"u": decline}}
    assert ws._storyteller_auto_link_candidates(7, client, [aligned_item("u", fingerprint=fingerprint)], [], set()) == expected
    assert create.call_count == expected
    assert client.get_kosync_content_hash.call_count == expected


def test_hash_match_join_claims_without_downloading(auto_link_env, monkeypatch, caplog):
    ws, db, client, store, create, claim = auto_link_env
    book = SimpleNamespace(abs_id="foreign", abs_title="Original", storyteller_uuid=None,
                           ebook_filename="original.epub", kosync_doc_id="a" * 32)
    db.get_book_by_kosync_id.return_value = book
    monkeypatch.setattr(ws, "_existing_mapping_access", lambda *args, **kwargs: "join")
    upsert = Mock()
    download = Mock()
    monkeypatch.setattr(ws, "_upsert_storyteller_mapping", upsert)
    monkeypatch.setattr(ws, "_download_storyteller_artifact", download)
    claimed = set()
    with caplog.at_level("INFO"):
        assert ws._storyteller_auto_link_candidates(7, client, [aligned_item("u")], [book], claimed) == 0
    assert claimed == {"foreign"}
    claim.assert_called_once_with(7, "foreign")
    upsert.assert_not_called()
    download.assert_not_called()
    assert "joined existing u for user 7" in caplog.text
    assert "Storyteller auto-link: linked" not in caplog.text


@pytest.fixture(autouse=True)
def isolate_storyteller_failure_logs(monkeypatch):
    from src import web_server as ws
    from src.api import storyteller_api
    from src.utils.logging_utils import PersistentConditionLogger
    conditions = PersistentConditionLogger()
    monkeypatch.setattr(ws, "get_persistent_condition_logger", lambda: conditions)
    monkeypatch.setattr(storyteller_api, "get_persistent_condition_logger", lambda: conditions)


def test_hourly_passes_cannot_overlap_for_a_reader(auto_link_env, monkeypatch, caplog):
    ws, db, client, store, create, claim = auto_link_env
    entered, release = threading.Event(), threading.Event()
    def library():
        entered.set()
        release.wait(2)
        return []
    client.list_library_books.side_effect = library
    db.get_all_books.return_value = []
    db.get_linked_abs_ids.return_value = []
    monkeypatch.setattr(ws, "uc", lambda: SimpleNamespace(storyteller_client=client))
    thread = threading.Thread(target=ws._storyteller_user_library_pass, args=(7,))
    thread.start()
    try:
        assert entered.wait(5)
        with caplog.at_level("INFO"):
            ws._storyteller_user_library_pass(7)
        assert client.list_library_books.call_count == 1
        assert "user 7 already scanning; skipping" in caplog.text
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert 7 not in ws._STORYTELLER_PASSES_IN_PROGRESS
    client.list_library_books.side_effect = None
    client.list_library_books.return_value = []
    ws._storyteller_user_library_pass(7)
    assert client.list_library_books.call_count == 2


def test_hourly_guard_is_per_reader(auto_link_env, monkeypatch):
    ws, db, client, store, create, claim = auto_link_env
    def library():
        if client.list_library_books.call_count == 1:
            ws._storyteller_user_library_pass(8)
        return []
    client.list_library_books.side_effect = library
    db.get_all_books.return_value = []
    db.get_linked_abs_ids.return_value = []
    monkeypatch.setattr(ws, "uc", lambda: SimpleNamespace(storyteller_client=client))
    ws._storyteller_user_library_pass(7)
    assert client.list_library_books.call_count == 2
    assert not ws._STORYTELLER_PASSES_IN_PROGRESS


def test_auto_link_stops_starting_books_after_budget(auto_link_env, monkeypatch, caplog):
    ws, db, client, store, create, claim = auto_link_env
    clock = [0]
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock[0])
    def slow_hash(*args):
        clock[0] = 601
        return None
    client.get_kosync_content_hash.side_effect = slow_hash
    with caplog.at_level("INFO"):
        ws._storyteller_auto_link_candidates(7, client, [aligned_item(f"u{i}") for i in range(8)], [], set())
    client.get_kosync_content_hash.assert_called_once_with("u0", "ebook")
    assert store["STORYTELLER_519_HASHES:7"]["_auto_cursor"] == 1
    assert "reached pass time budget; stopping" in caplog.text


def test_backfill_stops_starting_books_after_budget(real_db, monkeypatch, caplog):
    import src.services.storyteller_library_watch as watch
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    clock = [0]
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock[0])
    books = [real_db.save_book(Book(abs_id=f"b{i}", storyteller_uuid=f"abcd-{i}",
                                   ebook_filename=f"storyteller_abcd-{i}.epub")) for i in range(3)]
    client = Mock()
    def slow_hash(uuid, fmt):
        clock[0] = 601
        return "a" * 32 if fmt == "ebook" else "b" * 32
    client.get_kosync_content_hash.side_effect = slow_hash
    library = [aligned_item(b.storyteller_uuid) for b in books]
    with caplog.at_level("INFO"):
        scan_linked(real_db, client, books, 7, library=library, deadline=600)
    assert [c.args for c in client.get_kosync_content_hash.call_args_list] == [
        ("abcd-0", "ebook"), ("abcd-0", "readaloud")]
    assert real_db.get_json_setting("STORYTELLER_519_HASHES:7")["_cursor"] == 1
    assert "reached pass time budget; stopping" in caplog.text


def test_hourly_pass_shares_one_budget_with_backfill(auto_link_env, monkeypatch):
    ws, db, client, store, create, claim = auto_link_env
    clock = [0]
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock[0])
    book = SimpleNamespace(abs_id="mapped", abs_title="Mapped", storyteller_uuid="mapped",
                           ebook_filename="plain.epub", original_ebook_filename="plain.epub")
    def slow_create(item):
        clock[0] = 601
        return SimpleNamespace(abs_id="new", abs_title="New", storyteller_uuid="new")
    create.side_effect = slow_create
    client.list_library_books.return_value = [aligned_item("new"), aligned_item("mapped")]
    db.get_all_books.return_value = [book]
    db.get_linked_abs_ids.return_value = ["mapped"]
    monkeypatch.setattr(ws, "uc", lambda: SimpleNamespace(storyteller_client=client))
    ws._storyteller_user_library_pass(7)
    client.get_kosync_content_hash.assert_called_once_with("new", "ebook")


def test_hourly_failure_warnings_suppress_repeats_and_reset_on_recovery(auto_link_env, monkeypatch, caplog):
    ws, db, client, store, create, claim = auto_link_env
    client.list_library_books.side_effect = RuntimeError("offline")
    monkeypatch.setattr(ws, "uc", lambda: SimpleNamespace(storyteller_client=client))
    db.get_all_books.return_value = []
    db.get_linked_abs_ids.return_value = []
    with caplog.at_level("DEBUG"):
        ws._storyteller_user_library_pass(7)
        ws._storyteller_user_library_pass(7)
        failures = [r for r in caplog.records if "Storyteller hashes: pass failed" in r.message]
        assert [r.levelname for r in failures] == ["WARNING", "DEBUG"]
        assert all(r.exc_info for r in failures)
        assert not ws._STORYTELLER_PASSES_IN_PROGRESS
        client.list_library_books.side_effect = None
        client.list_library_books.return_value = []
        ws._storyteller_user_library_pass(7)
        client.list_library_books.side_effect = RuntimeError("offline again")
        ws._storyteller_user_library_pass(7)
    failures = [r for r in caplog.records if "Storyteller hashes: pass failed" in r.message]
    assert [r.levelname for r in failures] == ["WARNING", "DEBUG", "WARNING"]
    assert "pass recovered for user 7" in caplog.text


@pytest.mark.parametrize("status,content_range", [(401, None), (404, None), (206, "bytes 99-100/200")])
def test_hash_closes_rejected_responses(status, content_range, monkeypatch):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    client = hash_client(b"unused")
    rejected = FakeResponse(status, b"bad", {"Content-Range": content_range} if content_range else None)
    client.session.get.return_value = rejected
    client.session.get.side_effect = None
    assert client.get_kosync_content_hash("uuid", "ebook") is None
    assert rejected.closed


def test_hash_reauthenticates_mid_sequence_and_closes_401(monkeypatch):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    content = hashlib.shake_256(b"401-samples").digest(5000)
    client = hash_client(content)
    get = client.session.get.side_effect
    rejected = FakeResponse(401)
    used = False
    def expire_once(url, **kwargs):
        nonlocal used
        if kwargs["headers"]["Range"].startswith("bytes=1024-") and not used:
            used = True
            return rejected
        return get(url, **kwargs)
    client.session.get.side_effect = expire_once
    client._get_fresh_token.side_effect = ["first", "second"]
    assert client.get_kosync_content_hash("uuid", "ebook") == EbookParser._compute_koreader_hash_from_bytes(None, content)
    assert used and rejected.closed
    assert all(r.closed for r in client.responses)
    assert client._get_fresh_token.call_count == 2


def test_hash_failure_warnings_suppress_repeats_and_recover(monkeypatch, caplog):
    monkeypatch.setenv("KOSYNC_HASH_METHOD", "content")
    client = hash_client(b"book")
    success = client.session.get.side_effect
    client.session.get.side_effect = RuntimeError("offline")
    with caplog.at_level("DEBUG"):
        assert client.get_kosync_content_hash("uuid", "ebook") is None
        assert client.get_kosync_content_hash("uuid", "ebook") is None
        failures = [r for r in caplog.records if "Storyteller hash failed" in r.message]
        assert [r.levelname for r in failures] == ["WARNING", "DEBUG"]
        assert all(r.exc_info for r in failures)
        client.session.get.side_effect = success
        assert client.get_kosync_content_hash("uuid", "ebook")
    assert "Storyteller hash recovered" in caplog.text



def test_library_decode_warnings_suppress_repeats_and_recover(caplog):
    client = StorytellerAPIClient()
    response = Mock(status_code=200)
    response.json.side_effect = ValueError("bad JSON")
    client._make_request = Mock(return_value=response)
    with caplog.at_level("DEBUG"):
        assert client.list_library_books() is None
        assert client.list_library_books() is None
        failures = [r for r in caplog.records if "Storyteller library list decode failed" in r.message]
        assert [r.levelname for r in failures] == ["WARNING", "DEBUG"]
        response.json.side_effect = None
        response.json.return_value = []
        assert client.list_library_books() == []
    assert "Storyteller library list decode recovered" in caplog.text



def test_large_storyteller_state_is_not_exported_to_environment(real_db, monkeypatch):
    from src.utils.config_loader import ConfigLoader
    key = "STORYTELLER_519_HASHES:7"
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("STORYTELLER_API_URL", raising=False)
    state = {f"{i:08x}-1234-1234-1234-123456789abc": {
        "ebook": "a" * 32, "readaloud": "b" * 32, "fingerprint": "c" * 32} for i in range(1000)}
    real_db.set_json_setting(key, state)
    real_db.set_setting("STORYTELLER_API_URL", "http://storyteller.test")
    ConfigLoader.load_settings(real_db)
    assert key not in os.environ
    assert os.environ["STORYTELLER_API_URL"] == "http://storyteller.test"
    assert real_db.get_json_setting(key) == state


def test_bad_environment_row_does_not_stop_later_settings(monkeypatch):
    from src.utils.config_loader import ConfigLoader
    monkeypatch.delenv("STORYTELLER_API_URL", raising=False)
    db = Mock()
    db.get_all_settings.return_value = {"BAD=KEY": "bad", "STORYTELLER_API_URL": "http://storyteller.test"}
    ConfigLoader.load_settings(db)
    assert os.environ["STORYTELLER_API_URL"] == "http://storyteller.test"
