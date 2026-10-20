"""Regression coverage for Storyteller OPDS hashes and linked-book reconciliation (#519)."""

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.api.storyteller_api import StorytellerAPIClient
from src.services.storyteller_library_watch import register_hashes, scan_linked
from src.utils.ebook_utils import EbookParser


class FakeResponse:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def close(self):
        pass

    def iter_content(self, chunk_size=1024):
        for idx in range(0, len(self.content), chunk_size):
            yield self.content[idx:idx + chunk_size]


def hash_client(content, ignore_range=False):
    client = StorytellerAPIClient()
    client.base_url = "http://storyteller.test"
    client._get_fresh_token = Mock(return_value="token")

    def get(url, *, headers, params, timeout, stream):
        assert params["format"] in {"ebook", "readaloud"}
        assert stream
        if ignore_range:
            return FakeResponse(200, content)
        start = int(headers["Range"].split("=")[1].split("-")[0])
        if start >= len(content):
            return FakeResponse(416)
        end = min(len(content), start + 1024)
        return FakeResponse(206, content[start:end],
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


def test_200_fallback_hashes_streamed_full_body():
    contents = bytes(i % 256 for i in range(350000))
    client = hash_client(contents, ignore_range=True)
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        assert client.get_kosync_content_hash("a-uuid", "readaloud") == (
            EbookParser._compute_koreader_hash_from_bytes(None, contents))
    client.session.get.assert_called_once()


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
    return db, client, book, details, Mock(), state


def test_registers_plain_and_readaloud_for_artifact_only():
    for artifact, expected in ((True, 2), (False, 1)):
        db, client, book, details, adopt, _ = setup_registration(artifact=artifact)
        with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
            assert register_hashes(db, client, book, adopt, details, user_id=1)
        assert adopt.call_count == expected
        adopt.assert_any_call(book.abs_id, "a" * 32)
        if not artifact:
            client.get_kosync_content_hash.assert_called_once_with("abcd-1234", "ebook")


def test_conflicting_hash_is_never_repointed():
    db, client, book, details, adopt, _ = setup_registration(owner="other-book")
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        assert register_hashes(db, client, book, adopt, details, user_id=3)
    adopt.assert_called_once_with("ebook-1", "a" * 32)


def test_unchanged_fingerprint_skips_rehash_and_change_retains_old_hash():
    db, client, book, details, adopt, state = setup_registration()
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        register_hashes(db, client, book, adopt, details, user_id=1)
        client.get_kosync_content_hash.reset_mock()
        register_hashes(db, client, book, adopt, details, user_id=1)
        client.get_kosync_content_hash.assert_not_called()
        details["readaloud"]["fingerprint"] = "v2"
        client.get_kosync_content_hash.side_effect = (
            lambda uuid, fmt: {"ebook": "c" * 32, "readaloud": "d" * 32}[fmt]
        )
        register_hashes(db, client, book, adopt, details, user_id=1)
    assert ("ebook-1", "b" * 32) in [c.args for c in adopt.call_args_list]
    assert ("ebook-1", "d" * 32) in [c.args for c in adopt.call_args_list]
    assert state["STORYTELLER_519_HASHES:1"]["abcd-1234"]["fingerprint"] == "v2"


def test_reconciler_only_handles_passed_user_books():
    db, client, book, details, adopt, _ = setup_registration()
    other = SimpleNamespace(abs_id="other", storyteller_uuid="other-uuid")
    client.list_library_books.return_value = [
        {"uuid": "abcd-1234", **details},
        {"uuid": "other-uuid", **details},
    ]
    with patch.dict(os.environ, {"KOSYNC_HASH_METHOD": "content"}):
        scan_linked(db, client, [book], adopt, 17)
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
    assert scan.call_args.args[4] == 7


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
        scan_linked(db, client, books, Mock(), user_id=12)
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
