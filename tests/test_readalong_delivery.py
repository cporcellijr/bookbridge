"""Tests for delivering a
generated read-along EPUB into BookOrbit's audiobook entry.

Covers the "Placement decision" hard constraints: target folder
resolution from the audio entry's own real track files, refusal when the
audio source is not BookOrbit or its tracks are not locally resolvable,
idempotent replacement on a second run (fixed filename, no accumulation),
and -- explicitly, since it is the constraint a regression here would be
easiest to miss -- that ``Book.ebook_filename`` /
``Book.original_ebook_filename`` are never touched.

BookOrbit itself is faked at the client-object boundary
(``BookOrbitClient``/``BookOrbitSyncClient``/``BookOrbitAudioSyncClient``),
the same seam ``tests/test_bookorbit_readalong_coexistence.py`` fakes at for
the sync-manager side of this same feature — no HTTP involved. The generated
EPUB itself is produced for real via ``build_readalong_epub`` (Phases 1-4),
using the same tiny real-EPUB/real-ffmpeg-audio fixture pattern as
``tests/test_readalong_builder.py``, so these tests exercise the real
generate-then-place pipeline, not a mocked builder.
"""
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from src.db.models import Book
from src.services.readalong_delivery import (
    ReadalongDeliveryResult,
    ResolvedAudioSource,
    _readalong_filename,
    deliver_readalong_epub,
    resolve_audiobook_folder,
)
from src.utils.ebook_utils import EbookParser

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH -- delivery generates via build_readalong_epub, "
           "which always transcodes real audio (same skip as test_readalong_builder.py)",
)

_CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _opf() -> str:
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title>'
        '<dc:identifier id="id">urn:uuid:test-book-id</dc:identifier></metadata>'
        '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="ch1"/></spine></package>'
    )


def _write_epub(path: Path) -> str:
    """A minimal one-spine-item EPUB with two short sentences; returns its
    extracted text (matching what extract_text_and_map produces)."""
    body = b"<html><body><p>First sentence here. Second sentence follows.</p></body></html>"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", _CONTAINER_XML)
        z.writestr("OEBPS/content.opf", _opf())
        z.writestr("OEBPS/ch1.xhtml", body)
    return "First sentence here. Second sentence follows."


def _make_audio(folder: Path, name: str = "track_000", duration: float = 1.0) -> Path:
    """A tiny, real, silent mp3 ffmpeg can actually decode (same recipe as
    test_readalong_builder.py's _make_audio)."""
    folder.mkdir(parents=True, exist_ok=True)
    audio_path = folder / f"{name}.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", str(duration), str(audio_path),
        ],
        check=True,
    )
    return audio_path


class _FakeAlignmentService:
    """Same minimal AlignmentService double as test_readalong_builder.py /
    test_readalong_segments.py: a linear char<->time map fitted to the whole
    source text, which satisfies build_sentence_clips's fitted-EPUB guard via
    total_chars."""

    class _FakeDatabaseService:
        def __init__(self, total_chars: int):
            self._total_chars = total_chars

        def get_alignment_total_chars(self, abs_id: str) -> Optional[int]:
            return self._total_chars

    def __init__(self, total_chars: int, total_seconds: float):
        self._total_chars = total_chars
        self._total_seconds = total_seconds
        self.database_service = self._FakeDatabaseService(total_chars)

    def get_map_terminal_char(self, abs_id: str) -> Optional[int]:
        return self._total_chars

    def aligned_word_offsets(self, abs_id: str) -> Optional[list]:
        return None

    def get_time_for_char(self, abs_id: str, char_offset: int) -> Optional[float]:
        frac = max(0.0, min(1.0, char_offset / self._total_chars)) if self._total_chars else 0.0
        return frac * self._total_seconds

    def _get_segments(self, abs_id: str) -> Optional[list]:
        """None -- an unsegmented (single, in-order narration) map, matching
        the real AlignmentService._get_segments contract (Finding 3 fix)."""
        return None


class _FakeAudioSyncClient:
    def __init__(self, book_id):
        self._book_id = book_id
        self.calls = 0

    def resolve_bookorbit_book_id(self, book: Book):
        self.calls += 1
        return self._book_id


class _FakeEbookSyncClient:
    def __init__(self, book_id):
        self._book_id = book_id
        self.calls = 0

    def resolve_bookorbit_book_id(self, book: Book):
        self.calls += 1
        return self._book_id


class _RaisingAudioSyncClient:
    """Blows up if ever consulted -- used to prove deliver_readalong_epub
    short-circuits on the audio_source guard before touching BookOrbit."""

    def resolve_bookorbit_book_id(self, book: Book):
        raise AssertionError("resolve_bookorbit_book_id should not be called")


class _FakeBookOrbitClient:
    """Fakes just the surface readalong_delivery.py calls on BookOrbitClient."""

    def __init__(self, tracks, library_id=7, libraries=None):
        self._tracks = tracks
        self._detail = {"libraryId": library_id, "files": []}
        if libraries is None and tracks and tracks[0].get("absolute_path"):
            library_root = Path(tracks[0]["absolute_path"]).parent.parent
            libraries = [{"id": library_id, "folders": [{"path": str(library_root)}]}]
        self._libraries = libraries if libraries is not None else []
        self._sync_state = {"state": "unavailable", "unavailableReason": "no_media_overlay_epub"}
        self.scan_calls = []
        self.deleted_file_ids = []

    def get_audiobook_info(self, book_id):
        return {"tracks": self._tracks}

    def get_book_detail(self, book_id, force: bool = False):
        return self._detail

    def get_libraries(self):
        return self._libraries

    def scan_library(self, library_id) -> bool:
        self.scan_calls.append(library_id)
        # Simulate BookOrbit indexing the newly-placed file and enabling sync.
        self._sync_state = {"state": "enabled", "unavailableReason": None}
        return True

    def get_read_aloud_sync(self, book_id, force: bool = False):
        return self._sync_state

    @staticmethod
    def read_aloud_sync_is_active(sync) -> bool:
        return bool(sync) and str(sync.get("state") or "").lower() == "enabled"

    def delete_book_file(self, file_id) -> bool:
        self.deleted_file_ids.append(file_id)
        return True


def _setup(tmp: Path, monkeypatch, folder_name: str = "Some Audiobook (2020)"):
    """Common fixture: an EPUB under BOOKS_DIR, one real audio track under a
    single audiobook folder under AUDIOBOOKS_DIR, and env pointed at both."""
    books_dir = tmp / "books"
    audiobooks_dir = tmp / "audiobooks"
    monkeypatch.setenv("BOOKS_DIR", str(books_dir))
    monkeypatch.setenv("AUDIOBOOKS_DIR", str(audiobooks_dir))

    parser = EbookParser(books_dir=str(books_dir), epub_cache_dir=str(tmp / "cache"))
    epub_path = books_dir / "Test Book.epub"
    combined_text = _write_epub(epub_path)

    audio_folder = audiobooks_dir / folder_name
    track_path = _make_audio(audio_folder)

    alignment_service = _FakeAlignmentService(len(combined_text), total_seconds=10.0)
    book = Book(
        abs_id="abs1",
        abs_title="Test Book",
        ebook_filename="Test Book.epub",
        original_ebook_filename="Test Book.epub",
        audio_source="BookOrbit",
        audio_source_id="42",
    )
    tracks = [{"id": 1, "filename": track_path.name, "format": "mp3", "absolute_path": str(track_path)}]
    return parser, alignment_service, book, tracks, audio_folder


# ---------------------------------------------------------------------------
# Target folder resolution
# ---------------------------------------------------------------------------

def test_resolve_audiobook_folder_returns_shared_parent_and_track_paths(tmp_path):
    folder = tmp_path / "Some Book"
    t0 = _make_audio(folder, name="track_000")
    t1 = _make_audio(folder, name="track_001")
    client = _FakeBookOrbitClient(tracks=[
        {"absolute_path": str(t0)}, {"absolute_path": str(t1)},
    ])

    resolved = resolve_audiobook_folder(client, "42")

    assert isinstance(resolved, ResolvedAudioSource)
    assert resolved.folder == folder
    assert resolved.track_paths == [t0, t1]


def test_resolve_audiobook_folder_refuses_when_no_tracks(tmp_path):
    client = _FakeBookOrbitClient(tracks=[])
    assert resolve_audiobook_folder(client, "42") is None


def test_resolve_audiobook_folder_refuses_when_track_missing_absolute_path(tmp_path):
    client = _FakeBookOrbitClient(tracks=[{"absolute_path": None}])
    assert resolve_audiobook_folder(client, "42") is None


def test_resolve_audiobook_folder_refuses_when_file_does_not_exist(tmp_path):
    missing = tmp_path / "gone" / "track_000.mp3"
    client = _FakeBookOrbitClient(tracks=[{"absolute_path": str(missing)}])
    assert resolve_audiobook_folder(client, "42") is None


def test_resolve_audiobook_folder_refuses_when_tracks_span_different_folders(tmp_path):
    t0 = _make_audio(tmp_path / "folder_a", name="track_000")
    t1 = _make_audio(tmp_path / "folder_b", name="track_001")
    client = _FakeBookOrbitClient(tracks=[
        {"absolute_path": str(t0)}, {"absolute_path": str(t1)},
    ])
    assert resolve_audiobook_folder(client, "42") is None


# ---------------------------------------------------------------------------
# Refusals: audio source not BookOrbit / not resolvable
# ---------------------------------------------------------------------------

def test_deliver_refuses_when_audio_source_is_not_bookorbit(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, _ = _setup(tmp_path, monkeypatch)
    book.audio_source = "ABS"
    client = _FakeBookOrbitClient(tracks=tracks)

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _RaisingAudioSyncClient(), book,
    )

    assert result is None
    assert not client.scan_calls


def test_deliver_refuses_when_audio_entry_not_resolvable(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, _ = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks)

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient(None), book,
    )

    assert result is None
    assert not client.scan_calls


def test_deliver_refuses_when_audio_tracks_not_locally_resolvable(tmp_path, monkeypatch):
    parser, alignment_service, book, _tracks, _ = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=[{"absolute_path": str(tmp_path / "nope.mp3")}])

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
    )

    assert result is None
    assert not client.scan_calls


def test_deliver_refuses_when_resolved_folder_is_under_the_ebook_library_root(tmp_path, monkeypatch):
    """Hard constraint: never write into the ebook library
    folder. This exercises the defensive guard directly, simulating a
    resolution bug where a track's absolute_path lands under BOOKS_DIR."""
    parser, alignment_service, book, _tracks, _ = _setup(tmp_path, monkeypatch)
    bad_track = _make_audio(tmp_path / "books" / "Test Book", name="track_000")
    client = _FakeBookOrbitClient(tracks=[{"absolute_path": str(bad_track)}])

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
    )

    assert result is None
    assert not client.scan_calls
    assert not (tmp_path / "books" / "Test Book" / "Test Book.readalong.epub").exists()


def test_deliver_refuses_when_audio_sits_loose_in_the_audiobook_library_root(tmp_path, monkeypatch):
    """A book whose audio is a loose file in AUDIOBOOKS_DIR itself is refused
    outright rather than written to the library root.

    Measured on the real library: 89 of 819 BookOrbit audio entries are loose
    root-level files. BookOrbit groups a FOLDER's files into one entry, so a
    read-along written to the root is grouped with every other root-level
    book rather than its own -- which is how "Apex Prey 1" ended up with
    state unavailable / no_media_overlay_epub after leaving a stray EPUB in
    the library root. BookOrbit's own folderPath for such an entry is the
    audio FILE itself (verified: /audiobooks/01. Apex Prey (2025).m4b), so
    POST /books/{id}/files cannot rescue it either -- its destination would
    be a path nested under a regular file."""
    parser, alignment_service, book, _tracks, _ = _setup(tmp_path, monkeypatch)
    audiobooks_root = tmp_path / "audiobooks"
    loose_track = _make_audio(audiobooks_root, name="track_000")
    client = _FakeBookOrbitClient(tracks=[{"absolute_path": str(loose_track)}])

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
    )

    assert result is None
    assert not client.scan_calls
    # Nothing at all was written into the library root.
    assert not list(audiobooks_root.glob("*.epub"))


def test_deliver_refuses_when_audio_sits_loose_in_a_nested_library_root(tmp_path, monkeypatch):
    """Independent review, finding 2 (P1): the mount-root-equality guard
    above only catches a loose book at ``AUDIOBOOKS_DIR`` itself. BookOrbit
    can register a library one level further down (a "Shared Library"
    folder under the mount), and a loose book sitting directly in THAT
    folder resolves to the library's own shared root just the same way --
    BookOrbit's own ``/api/v1/libraries`` is the only source of truth for
    what counts as a library root, not any single env var. Without this
    fix, delivery would have written the EPUB straight into that shared
    folder and requested a scan, exactly the placement the root guard
    exists to refuse."""
    parser, alignment_service, book, _tracks, _ = _setup(tmp_path, monkeypatch)
    audiobooks_root = tmp_path / "audiobooks"
    shared_library_root = audiobooks_root / "Shared Library"
    loose_track = _make_audio(shared_library_root, name="track_000")
    # A second, independent loose book sitting in the SAME shared folder --
    # the whole reason writing there is unsafe, not just theoretically wrong.
    _make_audio(shared_library_root, name="another_book")
    client = _FakeBookOrbitClient(
        tracks=[{"absolute_path": str(loose_track)}],
        library_id=9,
        libraries=[{"id": 9, "folders": [{"path": str(shared_library_root)}]}],
    )

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
    )

    assert result is None
    assert not client.scan_calls
    assert not list(shared_library_root.glob("*.epub"))


# ---------------------------------------------------------------------------
# Happy path: writes into the audio folder, scans, confirms
# ---------------------------------------------------------------------------

def test_deliver_refuses_when_library_root_metadata_is_unavailable(tmp_path, monkeypatch):
    """An unknown BookOrbit ownership result must not permit a write."""
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks, libraries=[])

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
    )

    assert result is None
    assert not client.scan_calls
    assert not list(audio_folder.glob("*.readalong.epub"))


def test_deliver_refuses_malformed_library_metadata(tmp_path, monkeypatch):
    """Malformed detail, library, or folder data cannot authorize a destination."""
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    cases = [
        ([], [{"id": 7, "folders": []}]),
        ({"libraryId": 7}, [{"id": 7, "folders": 42}]),
        ({"libraryId": 7}, [{"id": 7, "folders": [{"path": "\x00"}]}]),
    ]
    for detail, libraries in cases:
        client = _FakeBookOrbitClient(tracks=tracks, libraries=libraries)
        client._detail = detail
        result = deliver_readalong_epub(
            parser, alignment_service, client,
            _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
        )
        assert result is None
        assert not client.scan_calls
    assert not list(audio_folder.glob("*.readalong.epub"))


def test_deliver_writes_into_audio_folder_and_confirms_enabled(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks, library_id=7)
    ebook_client = _FakeEbookSyncClient("ebook-1")
    audio_client = _FakeAudioSyncClient("audio-1")

    result = deliver_readalong_epub(
        parser, alignment_service, client, ebook_client, audio_client, book,
        confirm_poll_interval_seconds=0.01,
    )

    assert isinstance(result, ReadalongDeliveryResult)
    assert result.abs_id == "abs1"
    assert result.audio_book_id == "audio-1"
    assert result.ebook_book_id == "ebook-1"
    assert result.library_id == 7
    assert result.audio_folder == str(audio_folder)
    assert result.scan_triggered is True
    assert result.confirmed is True
    assert client.scan_calls == [7]

    output_path = Path(result.output_path)
    assert output_path.parent == audio_folder
    assert output_path.exists()
    assert output_path.name == _readalong_filename(tmp_path / "books" / "Test Book.epub")
    # It is a real, openable EPUB zip, not a placeholder.
    with zipfile.ZipFile(output_path) as zf:
        assert "mimetype" in zf.namelist()


def test_deliver_never_writes_into_the_ebook_library_folder(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks)

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
        confirm_poll_interval_seconds=0.01,
    )

    assert result is not None
    books_dir = tmp_path / "books"
    generated_files_in_ebook_dir = list(books_dir.rglob("*.readalong.epub"))
    assert generated_files_in_ebook_dir == []


# ---------------------------------------------------------------------------
# Idempotent replacement on a second run
# ---------------------------------------------------------------------------

def test_deliver_second_run_replaces_in_place_without_duplicating(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks)
    ebook_client = _FakeEbookSyncClient("ebook-1")
    audio_client = _FakeAudioSyncClient("audio-1")

    first = deliver_readalong_epub(
        parser, alignment_service, client, ebook_client, audio_client, book,
        confirm_poll_interval_seconds=0.01,
    )
    assert first is not None
    first_mtime = Path(first.output_path).stat().st_mtime_ns

    # Force a detectably different write on the second run (skip the OS mtime
    # granularity race) and confirm it landed at the exact same path.
    import time
    time.sleep(0.05)

    second = deliver_readalong_epub(
        parser, alignment_service, client, ebook_client, audio_client, book,
        confirm_poll_interval_seconds=0.01,
    )
    assert second is not None

    assert second.output_path == first.output_path
    second_mtime = Path(second.output_path).stat().st_mtime_ns
    assert second_mtime > first_mtime, "second run did not actually rewrite the file"

    readalong_files = list(audio_folder.glob("*.readalong.epub"))
    assert len(readalong_files) == 1, f"expected exactly one generated file, found {readalong_files}"


# ---------------------------------------------------------------------------
# Book.ebook_filename / original_ebook_filename are never mutated
# ---------------------------------------------------------------------------

def test_deliver_never_mutates_ebook_filename_fields(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, _ = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks)
    ebook_client = _FakeEbookSyncClient("ebook-1")
    audio_client = _FakeAudioSyncClient("audio-1")

    before_ebook_filename = book.ebook_filename
    before_original = book.original_ebook_filename

    result = deliver_readalong_epub(
        parser, alignment_service, client, ebook_client, audio_client, book,
        confirm_poll_interval_seconds=0.01,
    )

    assert result is not None
    assert book.ebook_filename == before_ebook_filename == "Test Book.epub"
    assert book.original_ebook_filename == before_original == "Test Book.epub"
    # The ebook-side entry was only *read* for reporting, never re-mapped.
    assert result.ebook_book_id == "ebook-1"
    assert ebook_client.calls == 1


def test_deliver_refusal_paths_also_never_mutate_ebook_filename_fields(tmp_path, monkeypatch):
    """Same guarantee on every refusal path, not just the happy path."""
    parser, alignment_service, book, tracks, _ = _setup(tmp_path, monkeypatch)
    book.audio_source = "ABS"
    before_ebook_filename = book.ebook_filename
    before_original = book.original_ebook_filename

    result = deliver_readalong_epub(
        parser, alignment_service, _FakeBookOrbitClient(tracks=tracks),
        _FakeEbookSyncClient("ebook-1"), _RaisingAudioSyncClient(), book,
    )

    assert result is None
    assert book.ebook_filename == before_ebook_filename
    assert book.original_ebook_filename == before_original


# ---------------------------------------------------------------------------
# Library id derivation: prefer detail's libraryId, fall back to folder match
# ---------------------------------------------------------------------------

def test_deliver_falls_back_to_library_folder_match_when_libraryid_missing(tmp_path, monkeypatch):
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(
        tracks=tracks,
        library_id=None,
        libraries=[{"id": 9, "folders": [{"path": str(audio_folder.parent)}]}],
    )

    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
        confirm_poll_interval_seconds=0.01,
    )

    assert result is not None
    assert result.library_id == 9
    assert client.scan_calls == [9]


def test_failed_chapter_preserves_delivered_book_and_does_not_scan(tmp_path, monkeypatch):
    """An unrecoverable chapter must not replace or publish an existing readalong."""
    parser, alignment_service, book, tracks, audio_folder = _setup(tmp_path, monkeypatch)
    source_path = Path(parser.resolve_book_path(book.ebook_filename))
    with zipfile.ZipFile(source_path) as source:
        members = {name: source.read(name) for name in source.namelist()}
    members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
        b"</manifest>",
        b'<item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/></manifest>',
    ).replace(b"</spine>", b'<itemref idref="ch2"/></spine>')
    members["OEBPS/ch2.xhtml"] = b"<html><body><p>Healthy chapter.</p></body></html>"
    with zipfile.ZipFile(source_path, "w") as source:
        for name, data in members.items():
            source.writestr(name, data)
    text, _ = parser.extract_text_and_map(source_path)
    alignment_service = _FakeAlignmentService(len(text), total_seconds=1.0)
    client = _FakeBookOrbitClient(tracks)
    output_path = audio_folder / _readalong_filename(Path(book.ebook_filename))
    previous = b"previous complete readalong"
    output_path.write_bytes(previous)

    from src.services.readalong_builder import _verify_marker_injection

    def reject_injection(original, modified, spine_index, href):
        if spine_index == 1:
            raise ValueError("chapter cannot preserve original text")
        return _verify_marker_injection(original, modified, spine_index, href)

    monkeypatch.setattr(
        "src.services.readalong_builder._verify_marker_injection", reject_injection,
    )
    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
        confirm_poll_interval_seconds=0.01,
    )

    assert result is None
    assert output_path.read_bytes() == previous
    assert client.scan_calls == []
    assert not list(audio_folder.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Staged progress reporting
# ---------------------------------------------------------------------------

def test_deliver_reports_resolving_audio_and_delivering_around_the_build(tmp_path, monkeypatch):
    """`deliver_readalong_epub` reports its own two stages -- 'resolving_audio'
    right away, 'delivering' once the build (which reports its own five
    stages in between) has returned -- around whatever `build_readalong_epub`
    itself reports, in the correct overall order."""
    parser, alignment_service, book, tracks, _audio_folder = _setup(tmp_path, monkeypatch)
    client = _FakeBookOrbitClient(tracks=tracks, library_id=7)

    seen: List[Tuple[str, float]] = []
    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _FakeAudioSyncClient("audio-1"), book,
        confirm_poll_interval_seconds=0.01,
        progress_callback=lambda stage, fraction: seen.append((stage, fraction)),
    )

    assert result is not None
    assert result.confirmed is True

    transitions: List[str] = []
    for stage, _fraction in seen:
        if not transitions or transitions[-1] != stage:
            transitions.append(stage)
    assert transitions == [
        "resolving_audio", "converting_epub", "parsing_epub", "transcoding_audio",
        "building_overlays", "packaging", "delivering",
    ]

    fractions = [fraction for _stage, fraction in seen]
    assert fractions == sorted(fractions)
    assert all(0.0 <= fraction <= 1.0 for fraction in fractions)


def test_deliver_refusal_before_build_still_reports_resolving_audio(tmp_path, monkeypatch):
    """A refusal that happens before `build_readalong_epub` is even called
    (audio source guard here) still reports 'resolving_audio' -- progress
    reporting doesn't depend on reaching the build step."""
    parser, alignment_service, book, tracks, _ = _setup(tmp_path, monkeypatch)
    book.audio_source = "ABS"
    client = _FakeBookOrbitClient(tracks=tracks)

    seen: List[Tuple[str, float]] = []
    result = deliver_readalong_epub(
        parser, alignment_service, client,
        _FakeEbookSyncClient("ebook-1"), _RaisingAudioSyncClient(), book,
        progress_callback=lambda stage, fraction: seen.append((stage, fraction)),
    )

    assert result is None
    assert [stage for stage, _fraction in seen] == ["resolving_audio"]
