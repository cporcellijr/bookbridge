"""Tests for the shared atomic download/copy helpers in src/utils/file_transfers.py.

These cover the transport-level guarantees every audio and ebook source now relies
on: a destination is replaced only by a complete, validated payload, and a failed
transfer leaves both the previous file and the directory listing untouched.
"""

import os
import sys
import zipfile

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.utils.file_transfers import (
    IncompleteTransferError,
    copy_file_to_path,
    hardlink_file_to_path,
    response_declares_size,
    stream_response_to_path,
)


class _Response:
    """Minimal stand-in for a streamed ``requests`` response."""

    def __init__(self, chunks, headers=None, status_code=200):
        self._chunks = chunks
        self.headers = headers if headers is not None else {}
        self.status_code = status_code

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def _staging_leftovers(directory):
    return [p.name for p in directory.iterdir() if p.name.endswith((".part", ".link"))]


# ---------------------------------------------------------------------------
# response_declares_size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers,expected", [
    ({"Content-Length": "12"}, 12),
    ({"Content-Length": "12", "Content-Encoding": "identity"}, 12),
    ({}, None),
    ({"Content-Length": "unknown"}, None),
    ({"Content-Length": ""}, None),
    ({"Content-Length": "12", "Content-Encoding": "gzip"}, None),
    ({"Content-Length": "12", "Content-Encoding": "BR"}, None),
])
def test_response_declares_size_only_trusts_comparable_lengths(headers, expected):
    assert response_declares_size(_Response([], headers)) == expected


def test_encoded_body_is_published_despite_length_mismatch(tmp_path):
    """A gzip body is decoded by requests, so Content-Length must not reject it."""
    target = tmp_path / "book.epub"
    response = _Response([b"A" * 5000], {"Content-Length": "12", "Content-Encoding": "gzip"})

    assert stream_response_to_path(
        response, target, expected_size=response_declares_size(response)
    ) is True
    assert target.stat().st_size == 5000


# ---------------------------------------------------------------------------
# stream_response_to_path
# ---------------------------------------------------------------------------

def test_complete_body_is_published(tmp_path):
    target = tmp_path / "track.m4b"
    assert stream_response_to_path(
        _Response([b"full", b" audio!!"], {"Content-Length": "12"}), target, expected_size=12
    ) is True
    assert target.read_bytes() == b"full audio!!"
    assert _staging_leftovers(tmp_path) == []


def test_missing_parent_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "deeper" / "track.m4b"
    assert stream_response_to_path(_Response([b"audio"]), target) is True
    assert target.read_bytes() == b"audio"


@pytest.mark.parametrize("chunks,expected_size,message", [
    ([b"partial"], 12, "got 7 bytes, expected 12"),
    ([], None, "empty payload"),
    ([b"too much data"], 4, "got 13 bytes, expected 4"),
])
def test_short_empty_and_oversized_bodies_are_rejected(tmp_path, chunks, expected_size, message):
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")

    with pytest.raises(IncompleteTransferError, match=message):
        stream_response_to_path(_Response(chunks), target, expected_size=expected_size)

    assert target.read_bytes() == b"original"
    assert _staging_leftovers(tmp_path) == []


def test_abrupt_disconnect_preserves_destination(tmp_path):
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")
    response = _Response([b"part", ConnectionError("download interrupted")])

    with pytest.raises(ConnectionError):
        stream_response_to_path(response, target, expected_size=12)

    assert target.read_bytes() == b"original"
    assert _staging_leftovers(tmp_path) == []


def test_failed_transfer_creates_no_destination(tmp_path):
    target = tmp_path / "track.m4b"
    with pytest.raises(IncompleteTransferError):
        stream_response_to_path(_Response([b"partial"]), target, expected_size=12)
    assert not target.exists()
    assert _staging_leftovers(tmp_path) == []


def test_min_size_rejects_an_error_page_before_publication(tmp_path):
    target = tmp_path / "book.epub"
    target.write_bytes(b"a real epub")

    with pytest.raises(IncompleteTransferError, match="expected more than 1024"):
        stream_response_to_path(_Response([b"<html>404</html>"]), target, min_size=1024)

    assert target.read_bytes() == b"a real epub"


def test_partial_content_response_is_rejected(tmp_path):
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")

    with pytest.raises(IncompleteTransferError, match="HTTP 206"):
        stream_response_to_path(_Response([b"range chunk"], status_code=206), target)

    assert target.read_bytes() == b"original"


def test_error_carries_byte_counts_for_diagnostics(tmp_path):
    with pytest.raises(IncompleteTransferError) as excinfo:
        stream_response_to_path(_Response([b"partial"]), tmp_path / "t.m4b", expected_size=12)
    assert (excinfo.value.actual_size, excinfo.value.expected_size) == (7, 12)


def test_destination_is_untouched_until_the_body_completes(tmp_path):
    """Concurrent readers must never observe a half-written destination."""
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")
    observed = []

    def chunks():
        yield b"full"
        observed.append(target.read_bytes())
        yield b" audio!!"
        observed.append(target.read_bytes())

    assert stream_response_to_path(_Response(chunks(), {"Content-Length": "12"}), target,
                                   expected_size=12) is True
    assert observed == [b"original", b"original"]
    assert target.read_bytes() == b"full audio!!"


def test_on_chunk_cancellation_preserves_destination(tmp_path):
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt("cancelled")

    with pytest.raises(KeyboardInterrupt):
        stream_response_to_path(_Response([b"one", b"two", b"three"]), target, on_chunk=cancel)

    assert target.read_bytes() == b"original"
    assert _staging_leftovers(tmp_path) == []


def test_validator_rejection_preserves_destination(tmp_path):
    target = tmp_path / "book.epub"
    target.write_bytes(b"good epub")

    def reject(staged):
        raise IncompleteTransferError(f"bad archive {staged}")

    with pytest.raises(IncompleteTransferError, match="bad archive"):
        stream_response_to_path(_Response([b"not a zip"]), target, validator=reject)

    assert target.read_bytes() == b"good epub"
    assert _staging_leftovers(tmp_path) == []


def test_validator_sees_the_staged_file_not_the_destination(tmp_path):
    target = tmp_path / "book.epub"
    seen = {}

    def check(staged):
        seen["path"] = staged
        seen["bytes"] = staged.read_bytes()

    assert stream_response_to_path(_Response([b"payload"]), target, validator=check) is True
    assert seen["bytes"] == b"payload"
    assert seen["path"] != target


def test_zip_validator_rejects_a_truncated_archive(tmp_path):
    """The Storyteller readaloud guard: a short EPUB must not reach the cache."""
    from src.api.storyteller_api import _validate_epub_zip

    good = tmp_path / "good.epub"
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    payload = good.read_bytes()

    target = tmp_path / "book.epub"
    target.write_bytes(b"previous good epub")

    with pytest.raises(IncompleteTransferError, match="not a valid zip"):
        stream_response_to_path(_Response([payload[:20]]), target, validator=_validate_epub_zip)

    assert target.read_bytes() == b"previous good epub"
    assert stream_response_to_path(_Response([payload]), target,
                                   validator=_validate_epub_zip) is True
    assert zipfile.ZipFile(target).read("mimetype") == b"application/epub+zip"


# ---------------------------------------------------------------------------
# copy_file_to_path / hardlink_file_to_path
# ---------------------------------------------------------------------------

def test_copy_publishes_a_complete_copy(tmp_path):
    source = tmp_path / "source.m4b"
    source.write_bytes(b"audio bytes")
    target = tmp_path / "dest" / "track.m4b"

    assert copy_file_to_path(source, target) is True
    assert target.read_bytes() == b"audio bytes"
    assert source.read_bytes() == b"audio bytes"


def test_copy_rejects_a_source_that_changes_mid_copy(tmp_path, monkeypatch):
    """A library file still being written must not be staged as complete."""
    import src.utils.file_transfers as file_transfers

    source = tmp_path / "source.m4b"
    source.write_bytes(b"first version")
    target = tmp_path / "track.m4b"
    target.write_bytes(b"original")

    real_open = open

    def growing_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(source):
            source.write_bytes(b"a much longer second version")
        return handle

    monkeypatch.setattr(file_transfers, "open", growing_open, raising=False)

    with pytest.raises(IncompleteTransferError, match="changed while being copied"):
        copy_file_to_path(source, target)

    assert target.read_bytes() == b"original"
    assert _staging_leftovers(tmp_path) == []


def test_copy_rejects_an_undersized_source(tmp_path):
    source = tmp_path / "source.epub"
    source.write_bytes(b"tiny")
    target = tmp_path / "book.epub"
    target.write_bytes(b"a real epub")

    with pytest.raises(IncompleteTransferError, match="expected more than 1024"):
        copy_file_to_path(source, target, min_size=1024)

    assert target.read_bytes() == b"a real epub"


def test_copy_missing_source_leaves_destination_intact(tmp_path):
    target = tmp_path / "book.epub"
    target.write_bytes(b"a real epub")

    with pytest.raises(FileNotFoundError):
        copy_file_to_path(tmp_path / "missing.epub", target)

    assert target.read_bytes() == b"a real epub"
    assert _staging_leftovers(tmp_path) == []


def test_hardlink_publishes_and_shares_the_inode(tmp_path):
    source = tmp_path / "source.m4b"
    source.write_bytes(b"audio bytes")
    target = tmp_path / "dest" / "track.m4b"

    assert hardlink_file_to_path(source, target) is True
    assert target.stat().st_ino == source.stat().st_ino
    assert _staging_leftovers(tmp_path) == []


def test_failed_hardlink_preserves_the_previous_staged_file(tmp_path):
    target = tmp_path / "track.m4b"
    target.write_bytes(b"previously staged")

    with pytest.raises(OSError):
        hardlink_file_to_path(tmp_path / "missing.m4b", target)

    assert target.read_bytes() == b"previously staged"
    assert _staging_leftovers(tmp_path) == []
