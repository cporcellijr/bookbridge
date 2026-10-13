"""Deliver a generated
read-along EPUB into BookOrbit.

**Placement (load-bearing, do not deviate without re-deciding):**
the generated EPUB goes into the AUDIOBOOK's own BookOrbit entry, as a
secondary file written into that entry's folder on the shared ``/audiobooks``
mount, beside its real audio track(s) -- never into the ebook library folder,
never rewritten as an entry's primary ebook, and never touching
``Book.ebook_filename`` / ``Book.original_ebook_filename``. Doing this keeps
KOReader on the original EPUB and leaves the bridge's own ebook<->BookOrbit
mapping completely alone; only BookOrbit's own internal read-along sync
(dormant unless both formats resolve to the SAME entry -- see
``SyncManager._mark_bookorbit_readalong_mirror``) starts firing, and it does
so on a *different* entry than the one the bridge maps as this book's ebook,
so that guard stays dormant by construction.

**Why a new module instead of extending ``readalong_builder``:** Phases 1-4
are pure, filesystem-only EPUB generation -- paths in, bytes out, no network
calls, no notion that BookOrbit exists beyond being a source of file paths.
This step is delivery: it talks to the live BookOrbit API to resolve which
filesystem folder an audio entry's tracks actually live in, discover which
library to rescan, and confirm the scan's outcome. Keeping that live-API
surface out of ``readalong_builder`` keeps that module's own tests
network-free, and keeps this module's own tests focused on refusal/placement
logic without dragging in SMIL/OPF assembly.

**Idempotent replacement:** the generated file's name is derived
deterministically from the source EPUB's filename
(:func:`_readalong_filename`), so regenerating a book overwrites the same
path rather than accumulating a new file each run --
:func:`~src.services.readalong_builder.build_readalong_epub` repackages via
``zipfile.ZipFile(output_path, "w")``, which truncates and rewrites the file
in place, and BookOrbit's own incremental scan then re-indexes the same
relative path as an update rather than a new file.
"""
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, List, Optional, Tuple

from src.api.bookorbit_client import _AUDIO_FORMATS, BookOrbitUploadError
from src.services.audio_source_adapters import BookOrbitAudioSourceAdapter
from src.services.readalong_builder import (
    ReadalongBuildResult,
    ReadalongProgressCallback,
    _STAGE_START,
    _safe_progress,
    build_readalong_epub,
)

if TYPE_CHECKING:
    from src.api.bookorbit_client import BookOrbitClient
    from src.db.models import Book
    from src.services.alignment_service import AlignmentService
    from src.sync_clients.bookorbit_audio_sync_client import BookOrbitAudioSyncClient
    from src.sync_clients.bookorbit_sync_client import BookOrbitSyncClient
    from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)

# Suffix appended to the source EPUB's own stem to name the generated file.
# Fixed per book (not timestamped) so regeneration replaces the same path
# instead of accumulating copies.
_READALONG_SUFFIX = ".readalong.epub"

# Default budget for confirming a triggered scan actually indexed the file,
# not just that BookOrbit accepted the request (its own response is a bare
# 202 -- see BookOrbitClient.scan_library).
_DEFAULT_CONFIRM_TIMEOUT_SECONDS = 60.0
_DEFAULT_CONFIRM_POLL_INTERVAL_SECONDS = 2.0


def _audiobooks_root() -> Path:
    """The bridge's own ``AUDIOBOOKS_DIR`` mount, read per call (settings can
    change without a restart -- CLAUDE.md's settings-system rule)."""
    return Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))


def _books_root() -> Path:
    """The bridge's own ebook library mount (``BOOKS_DIR``), read per call.

    Used only as a defensive guard: the generated file must never land here,
    even if a resolution bug ever pointed the audio folder somewhere wrong.
    """
    return Path(os.environ.get("BOOKS_DIR", "/books"))


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` itself or nested under it."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_generated_readalong_epub(filename: str) -> bool:
    """Whether ``filename`` is a generated read-along (not a source ebook)."""
    return filename.lower().endswith(_READALONG_SUFFIX)


def _readalong_filename(epub_path: Path) -> str:
    """Deterministic generated filename for a given source EPUB.

    Tied to the source EPUB's own stem (not the abs_id) so the file is
    self-describing inside the audiobook folder; fixed across regenerations
    of the same book so a second run replaces this exact path.
    """
    return f"{epub_path.stem}{_READALONG_SUFFIX}"


@dataclass(frozen=True)
class ResolvedAudioSource:
    """One BookOrbit audio entry's real, locally-resolvable track files.

    ``folder`` is the single directory every track in ``track_paths`` shares
    -- the audiobook's own folder BookOrbit groups that entry's files by, and
    the destination for the generated read-along EPUB.
    """
    folder: Path
    track_paths: List[Path]


def resolve_audiobook_folder(
    bookorbit_client: "BookOrbitClient", audio_book_id: object,
) -> Optional[ResolvedAudioSource]:
    """Resolve a BookOrbit audio entry's real track files and their shared folder.

    Returns ``None`` -- never raises -- when the entry has no tracks, a track
    has no ``absolute_path`` (BookOrbit could not resolve one locally, so the
    caller would have to download bytes with no folder to write into), a
    resolved path is not an existing file, or the tracks disagree about which
    folder they live in (unexpected for a normal BookOrbit entry, and not
    safe to guess a destination from).
    """
    info = bookorbit_client.get_audiobook_info(audio_book_id)
    tracks = (info or {}).get("tracks") or []
    if not tracks:
        return None

    track_paths: List[Path] = []
    for track in tracks:
        raw = (track or {}).get("absolute_path")
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_file():
            return None
        track_paths.append(candidate)

    folders = {p.parent for p in track_paths}
    if len(folders) != 1:
        logger.error(
            "BookOrbit audio entry %s: tracks span %d different folders (%s); "
            "refusing to guess a read-along destination",
            audio_book_id, len(folders), sorted(str(f) for f in folders),
        )
        return None

    return ResolvedAudioSource(folder=next(iter(folders)), track_paths=track_paths)


def _resolve_library_id(
    bookorbit_client: "BookOrbitClient", audio_book_id: object, audio_folder: Path,
) -> Optional[int]:
    """The BookOrbit library id owning ``audio_folder``.

    Prefers the audio entry's own book-detail ``libraryId`` (present on this
    install's BookOrbit v3 server, and free -- the detail is already
    TTL-cached from :func:`resolve_audiobook_folder`'s own
    ``get_audiobook_info`` call). Falls back to matching ``audio_folder``
    against ``GET /api/v1/libraries``'s advertised ``folders[].path`` for a
    server/response shape that omits it -- never observed here, but cheap
    insurance against hardcoding a single library id.
    """
    detail = bookorbit_client.get_book_detail(audio_book_id)
    library_id = (detail or {}).get("libraryId")
    if library_id is not None:
        return library_id

    for library in bookorbit_client.get_libraries():
        if not isinstance(library, dict):
            continue
        for folder in library.get("folders") or []:
            folder_path = (folder or {}).get("path")
            if not folder_path:
                continue
            if _is_within(audio_folder, Path(folder_path)):
                return library.get("id")
    return None


def _is_bookorbit_library_root(
    bookorbit_client: "BookOrbitClient", audio_book_id: object, folder: Path,
) -> bool:
    """Whether ``folder`` is unsafe for a generated read-along.

    Queries BookOrbit's advertised folder roots and the audio entry's
    ``libraryId``. A failed, empty, malformed, or unrelated lookup fails
    closed; a book-owned folder is safe only beneath its owning root.
    """
    try:
        detail = bookorbit_client.get_book_detail(audio_book_id) or {}
        libraries = bookorbit_client.get_libraries()
    except Exception:
        logger.warning(
            "BookOrbit: could not inspect library roots for read-along delivery",
            exc_info=True,
        )
        return True
    if not isinstance(detail, dict):
        logger.warning(
            "BookOrbit: audio entry metadata is malformed; refusing read-along delivery"
        )
        return True
    if not isinstance(libraries, list) or not libraries:
        logger.warning(
            "BookOrbit: library root metadata unavailable; refusing read-along delivery"
        )
        return True

    owning_library_id = detail.get("libraryId")
    if owning_library_id is not None:
        libraries = [
            library for library in libraries
            if isinstance(library, dict)
            and str(library.get("id")) == str(owning_library_id)
        ]
        if not libraries:
            logger.warning(
                "BookOrbit: library metadata does not include owning library %s; "
                "refusing read-along delivery",
                owning_library_id,
            )
            return True

    try:
        normalized_folder = folder.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        logger.warning(
            "BookOrbit: could not normalize resolved audio folder '%s'; "
            "refusing read-along delivery",
            folder,
            exc_info=True,
        )
        return True
    folder_is_under_advertised_root = False
    for library in libraries:
        if not isinstance(library, dict):
            continue
        folders = library.get("folders")
        if not isinstance(folders, list):
            continue
        for lib_folder in folders:
            if not isinstance(lib_folder, dict) or not lib_folder.get("path"):
                continue
            try:
                root = Path(lib_folder["path"]).resolve(strict=False)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if normalized_folder == root:
                return True
            if _is_within(normalized_folder, root):
                folder_is_under_advertised_root = True

    if not folder_is_under_advertised_root:
        logger.warning(
            "BookOrbit: resolved audio folder '%s' is outside its owning library "
            "roots; refusing read-along delivery",
            folder,
        )
        return True
    return False


def _local_delivery_blocker(
    bookorbit_client: "BookOrbitClient", audio_book_id: object,
    resolved: Optional[ResolvedAudioSource],
) -> Optional[str]:
    """Why a direct write beside the audio is not allowed, or ``None`` if it is."""
    if resolved is None:
        return "tracks are not visible at the same path in this container"
    audiobooks_root = _audiobooks_root()
    if not _is_within(resolved.folder, audiobooks_root):
        return f"audio folder '{resolved.folder}' is not under AUDIOBOOKS_DIR"
    if _is_within(resolved.folder, _books_root()):
        return f"audio folder '{resolved.folder}' is under the ebook library root"
    if resolved.folder == audiobooks_root or _is_bookorbit_library_root(
        bookorbit_client, audio_book_id, resolved.folder,
    ):
        return f"audio folder '{resolved.folder}' is a shared library root or unverified"
    return None


def _upload_destination_is_safe(
    bookorbit_client: "BookOrbitClient", audio_book_id: object,
) -> Optional[str]:
    """The entry's own ``folderPath`` when it is a dedicated book folder, else ``None``.

    Judged purely from BookOrbit's metadata (BookOrbit paths vs BookOrbit
    paths): a folderPath that is an audio file (loose root-level book) or equals
    a library root, or any missing/malformed metadata, fails closed.
    """
    try:
        detail = bookorbit_client.get_book_detail(audio_book_id)
        libraries = bookorbit_client.get_libraries()
    except Exception:
        logger.warning(
            "BookOrbit: could not inspect library roots for read-along delivery",
            exc_info=True,
        )
        return None
    folder_path = detail.get("folderPath") if isinstance(detail, dict) else None
    if not isinstance(folder_path, str) or not folder_path.strip() or not isinstance(libraries, list):
        return None
    folder = PurePosixPath(folder_path.rstrip("/") or "/")
    track_names = {
        f.get("filename") for f in detail.get("files") or [] if isinstance(f, dict)
    }
    if folder.name in track_names or folder.suffix.lower().lstrip(".") in _AUDIO_FORMATS:
        return None
    owner = detail.get("libraryId")
    under_root = False
    for lib in libraries:
        if not isinstance(lib, dict) or (owner is not None and str(lib.get("id")) != str(owner)):
            continue
        for lib_folder in lib.get("folders") if isinstance(lib.get("folders"), list) else []:
            raw = lib_folder.get("path") if isinstance(lib_folder, dict) else None
            if not isinstance(raw, str) or not raw:
                continue
            root = PurePosixPath(raw.rstrip("/") or "/")
            if folder == root:
                return None
            if root in folder.parents:
                under_root = True
    return folder_path if under_root else None


def _upload_error_text(error: Optional[BookOrbitUploadError], size_mb: float) -> str:
    """The operator-facing reason for a refused upload."""
    if error is not None and error.kind == "too_large":
        return (
            f"the read-along EPUB is {size_mb:.0f} MB and the request was refused by "
            "BookOrbit's maximum upload size (BookOrbit Settings) or a reverse "
            "proxy/tunnel in front of it; the alternative is a shared local mount so "
            "BookBridge can deliver directly"
        )
    return (
        f"BookOrbit refused the upload (kind={getattr(error, 'kind', None)}, "
        f"status={getattr(error, 'status_code', None)})"
    )


def _upload_replacing(
    bookorbit_client: "BookOrbitClient", audio_book_id: object, staged: Path, filename: str,
) -> Tuple[bool, Optional[BookOrbitUploadError]]:
    """Upload ``staged``; on a name conflict delete the stale row and retry once.

    ``duplicate`` (identical bytes already attached) counts as delivered.
    """
    book_id = int(audio_book_id) if str(audio_book_id).isdigit() else audio_book_id
    error: Optional[BookOrbitUploadError] = None
    for attempt in (1, 2):
        try:
            bookorbit_client.upload_book_file(book_id, staged, filename)
            return True, None
        except BookOrbitUploadError as e:
            error = e
        if error.kind == "duplicate":
            return True, None
        if error.kind != "name_conflict" or attempt == 2:
            break
        detail = bookorbit_client.get_book_detail(audio_book_id, force=True)
        stale = next(
            (f.get("id") for f in (detail or {}).get("files") or []
             if isinstance(f, dict) and f.get("filename") == filename),
            None,
        )
        if stale is None or not bookorbit_client.delete_book_file(stale):
            break
    return False, error


def _deliver_by_upload(
    parser: "EbookParser",
    alignment_service: "AlignmentService",
    bookorbit_client: "BookOrbitClient",
    ebook_sync_client: "BookOrbitSyncClient",
    book: "Book",
    audio_book_id: object,
    epub_path: Path,
    confirm_timeout_seconds: float,
    confirm_poll_interval_seconds: float,
    progress_callback: Optional[ReadalongProgressCallback],
) -> Optional["ReadalongDeliveryResult"]:
    """Build in a staging dir and upload into the audio entry via BookOrbit's API.

    Used when the audio folder cannot be written directly (mismatched mounts,
    or a folder inside the shared ebook library). Always removes the staging file.
    """
    abs_id = book.abs_id
    if _upload_destination_is_safe(bookorbit_client, audio_book_id) is None:
        logger.error(
            "🚫 Refusing read-along delivery for '%s': BookOrbit audio entry %s is a "
            "shared BookOrbit library root or its ownership could not be verified; "
            "move loose audio into its own book folder under the owning library root "
            "and rescan before regenerating",
            abs_id, audio_book_id,
        )
        return None

    resolved = resolve_audiobook_folder(bookorbit_client, audio_book_id)
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    if resolved is not None:
        audio_paths = resolved.track_paths
    else:
        adapter = BookOrbitAudioSourceAdapter(bookorbit_client, data_dir)
        try:
            audio_paths = [
                Path(f["local_path"])
                for f in adapter.get_audio_files(str(audio_book_id), abs_id)
            ]
        except Exception:
            logger.error(
                "🚫 Refusing read-along delivery for '%s': could not download the audio "
                "tracks of BookOrbit entry %s", abs_id, audio_book_id, exc_info=True,
            )
            return None
        if not audio_paths:
            logger.warning(
                "🚫 Refusing read-along delivery for '%s': BookOrbit audio entry %s has no tracks",
                abs_id, audio_book_id,
            )
            return None

    filename = _readalong_filename(epub_path)
    staged = data_dir / "readalong_staging" / re.sub(r"[^\w.-]", "_", str(abs_id)) / filename
    staged.parent.mkdir(parents=True, exist_ok=True)
    try:
        build = build_readalong_epub(
            parser=parser, alignment_service=alignment_service, epub_path=epub_path,
            audio_paths=audio_paths, abs_id=abs_id, output_path=staged,
            progress_callback=progress_callback,
        )
        if build is None:
            return None
        _safe_progress(progress_callback, "delivering", _STAGE_START["delivering"])
        size_mb = staged.stat().st_size / (1024 * 1024)
        uploaded, error = _upload_replacing(bookorbit_client, audio_book_id, staged, filename)
    finally:
        staged.unlink(missing_ok=True)
        try:
            staged.parent.rmdir()
        except OSError:
            pass
    if not uploaded:
        logger.error(
            "🚫 Read-along upload for '%s' failed: %s",
            abs_id, _upload_error_text(error, size_mb),
        )
        return None

    read_aloud_sync = _wait_for_read_aloud_sync(
        bookorbit_client, audio_book_id, confirm_timeout_seconds, confirm_poll_interval_seconds,
    )
    confirmed = bookorbit_client.read_aloud_sync_is_active(read_aloud_sync)
    try:
        ebook_book_id = ebook_sync_client.resolve_bookorbit_book_id(book)
    except Exception as e:
        logger.debug(
            "Read-along delivery for '%s': could not resolve the bridge's ebook-side "
            "BookOrbit entry for reporting: %s", abs_id, e, exc_info=True,
        )
        ebook_book_id = None
    library_id = (bookorbit_client.get_book_detail(audio_book_id) or {}).get("libraryId")
    if confirmed:
        logger.info(
            "📖 Delivered read-along EPUB for '%s' to BookOrbit audio entry %s "
            "(uploaded, %.0f MB) -- readAloudSync confirmed enabled", abs_id, audio_book_id, size_mb,
        )
    else:
        logger.warning(
            "⚠️ Uploaded read-along EPUB for '%s' to BookOrbit audio entry %s "
            "but could not confirm readAloudSync enabled (last status=%s)",
            abs_id, audio_book_id, read_aloud_sync,
        )
    return ReadalongDeliveryResult(
        abs_id=abs_id, audio_book_id=audio_book_id, ebook_book_id=ebook_book_id,
        library_id=library_id, audio_folder=None, output_path=filename,
        scan_triggered=False, confirmed=confirmed, read_aloud_sync=read_aloud_sync,
        build=build, delivery="upload",
    )


def _wait_for_read_aloud_sync(
    bookorbit_client: "BookOrbitClient",
    audio_book_id,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> Optional[dict]:
    """Poll BookOrbit's read-along sync status until it reports ``enabled`` or
    ``timeout_seconds`` elapses.

    Returns the last-seen payload either way -- including a still-not-enabled
    one on timeout -- so the caller reports the real outcome instead of
    assuming a triggered scan worked (the scan endpoint itself only confirms
    the request was accepted, not that it finished).
    """
    deadline = time.monotonic() + timeout_seconds
    sync = bookorbit_client.get_read_aloud_sync(audio_book_id, force=True)
    while not bookorbit_client.read_aloud_sync_is_active(sync):
        if time.monotonic() >= deadline:
            return sync
        time.sleep(poll_interval_seconds)
        sync = bookorbit_client.get_read_aloud_sync(audio_book_id, force=True)
    return sync


@dataclass(frozen=True)
class ReadalongDeliveryResult:
    """The full outcome of :func:`deliver_readalong_epub`.

    ``delivery`` is ``'local'`` (written beside the audio on a shared mount,
    then a library scan) or ``'upload'`` (sent through BookOrbit's API; no
    scan). For ``'upload'``, ``audio_folder`` is ``None`` and ``output_path``
    is the uploaded filename inside the BookOrbit entry, not a local path.

    ``confirmed`` is ``True`` only when BookOrbit itself reported
    ``readAloudSync.state == 'enabled'`` for ``audio_book_id`` before the
    confirmation timeout -- a scan that was merely *accepted*
    (``scan_triggered``) is not the same claim. ``read_aloud_sync`` is the
    last-seen payload either way, so a caller can log/report the real reason
    (``unavailableReason``) when confirmation did not succeed.
    """
    abs_id: str
    audio_book_id: object
    ebook_book_id: Optional[object]
    library_id: Optional[int]
    audio_folder: Optional[str]
    output_path: str
    scan_triggered: bool
    confirmed: bool
    read_aloud_sync: Optional[dict]
    build: ReadalongBuildResult
    delivery: str = "local"


def deliver_readalong_epub(
    parser: "EbookParser",
    alignment_service: "AlignmentService",
    bookorbit_client: "BookOrbitClient",
    ebook_sync_client: "BookOrbitSyncClient",
    audio_sync_client: "BookOrbitAudioSyncClient",
    book: "Book",
    confirm_timeout_seconds: float = _DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    confirm_poll_interval_seconds: float = _DEFAULT_CONFIRM_POLL_INTERVAL_SECONDS,
    progress_callback: Optional[ReadalongProgressCallback] = None,
) -> Optional[ReadalongDeliveryResult]:
    """Generate (Phases 1-4) and deliver (Phase 5) a read-along EPUB for ``book``.

    Refuses -- returns ``None``, never raises for an expected refusal -- when:
    ``book.audio_source`` is not ``'BookOrbit'``; the audio side has no
    resolvable BookOrbit entry; that entry's tracks are not locally
    resolvable files (see :func:`resolve_audiobook_folder`); the resolved
    folder is not under this bridge's own ``AUDIOBOOKS_DIR``, or is (somehow)
    under ``BOOKS_DIR`` -- the ebook library root must never receive this
    file; the resolved folder IS ``AUDIOBOOKS_DIR`` itself, i.e. the audio is
    a loose file in the library root with no folder of its own to be grouped
    by; the book has no ebook filename, or that file cannot be located on
    disk; or Phase 3/4's own :func:`~src.services.readalong_builder.build_readalong_epub`
    refuses (no alignment map fitted to this EPUB, no audio, etc. -- see that
    function's own docstring for the full list).

    Never touches ``book.ebook_filename`` / ``book.original_ebook_filename``,
    never writes into the ebook library folder, and never calls anything that
    would make the generated file an entry's primary ebook -- the write is a
    plain file write into the audio entry's own existing folder, sibling to
    its real track(s). The bridge's ebook<->BookOrbit mapping is read here
    (``ebook_sync_client.resolve_bookorbit_book_id``) only to report which
    entry it resolves to, never to change it.

    :param parser: source of the book's spine text/DOM (Phase 1/2 dependency).
    :param alignment_service: source of the book's stored alignment map.
    :param bookorbit_client: raw BookOrbit API client (detail, scan, files).
    :param ebook_sync_client: resolves the bridge's ebook-side BookOrbit entry
        for ``book`` -- read-only, reported in the result for verification.
    :param audio_sync_client: resolves the bridge's audio-side BookOrbit
        entry for ``book`` -- the entry this delivers into.
    :param book: the mapped book to generate and deliver a read-along for.
    :param confirm_timeout_seconds: how long to poll BookOrbit for
        ``readAloudSync.state == 'enabled'`` after triggering a scan.
    :param confirm_poll_interval_seconds: delay between confirmation polls.
    :param progress_callback: optional ``(stage, fraction)`` reporter,
        threaded straight through to :func:`~src.services.readalong_builder.build_readalong_epub`
        for its own stages -- see ``readalong_builder``'s module-level
        ``_STAGE_START`` map. A failure in the callback itself never aborts
        delivery (``_safe_progress`` swallows it).
    :return: the delivery result, or ``None`` if refused.
    """
    abs_id = book.abs_id
    _safe_progress(progress_callback, "resolving_audio", _STAGE_START["resolving_audio"])

    if getattr(book, "audio_source", None) != "BookOrbit":
        logger.warning(
            "🚫 Refusing read-along delivery for '%s': audio source is '%s', not BookOrbit",
            abs_id, getattr(book, "audio_source", None),
        )
        return None

    audio_book_id = audio_sync_client.resolve_bookorbit_book_id(book)
    if audio_book_id is None:
        logger.warning(
            "🚫 Refusing read-along delivery for '%s': no resolvable BookOrbit audio entry",
            abs_id,
        )
        return None

    resolved = resolve_audiobook_folder(bookorbit_client, audio_book_id)
    local_blocker = _local_delivery_blocker(bookorbit_client, audio_book_id, resolved)
    if local_blocker is not None:
        logger.info(
            "📤 Read-along for '%s': local delivery not possible (%s); uploading into "
            "BookOrbit entry %s", abs_id, local_blocker, audio_book_id,
        )
        resolved = None

    epub_filename = getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)
    if not epub_filename:
        logger.warning(
            "🚫 Refusing read-along delivery for '%s': book has no ebook filename to source text from",
            abs_id,
        )
        return None
    try:
        epub_path = parser.resolve_book_path(epub_filename)
    except FileNotFoundError:
        logger.warning(
            "🚫 Refusing read-along delivery for '%s': could not locate source EPUB '%s' on disk",
            abs_id, epub_filename, exc_info=True,
        )
        return None

    if resolved is None:
        return _deliver_by_upload(
            parser, alignment_service, bookorbit_client, ebook_sync_client, book,
            audio_book_id, Path(epub_path), confirm_timeout_seconds,
            confirm_poll_interval_seconds, progress_callback,
        )

    output_path = resolved.folder / _readalong_filename(Path(epub_path))

    build = build_readalong_epub(
        parser=parser,
        alignment_service=alignment_service,
        epub_path=epub_path,
        audio_paths=resolved.track_paths,
        abs_id=abs_id,
        output_path=output_path,
        progress_callback=progress_callback,
    )
    if build is None:
        # build_readalong_epub already logged the specific refusal reason.
        return None

    _safe_progress(progress_callback, "delivering", _STAGE_START["delivering"])
    library_id = _resolve_library_id(bookorbit_client, audio_book_id, resolved.folder)
    scan_triggered = False
    if library_id is not None:
        scan_triggered = bookorbit_client.scan_library(library_id)
    else:
        logger.error(
            "⚠️ Read-along delivery for '%s': wrote '%s' but could not determine which "
            "BookOrbit library owns it -- trigger a manual scan",
            abs_id, output_path,
        )

    read_aloud_sync = None
    confirmed = False
    if scan_triggered:
        read_aloud_sync = _wait_for_read_aloud_sync(
            bookorbit_client, audio_book_id, confirm_timeout_seconds, confirm_poll_interval_seconds,
        )
        confirmed = bookorbit_client.read_aloud_sync_is_active(read_aloud_sync)

    try:
        ebook_book_id = ebook_sync_client.resolve_bookorbit_book_id(book)
    except Exception as e:
        logger.debug(
            "Read-along delivery for '%s': could not resolve the bridge's ebook-side "
            "BookOrbit entry for reporting: %s", abs_id, e, exc_info=True,
        )
        ebook_book_id = None

    if confirmed:
        logger.info(
            "📖 Delivered read-along EPUB for '%s' to BookOrbit audio entry %s "
            "(library=%s, folder='%s') -- readAloudSync confirmed enabled",
            abs_id, audio_book_id, library_id, resolved.folder,
        )
    else:
        logger.warning(
            "⚠️ Wrote read-along EPUB for '%s' to BookOrbit audio entry %s (library=%s, "
            "folder='%s') but could not confirm readAloudSync enabled (scan_triggered=%s, "
            "last status=%s)",
            abs_id, audio_book_id, library_id, resolved.folder, scan_triggered, read_aloud_sync,
        )

    return ReadalongDeliveryResult(
        abs_id=abs_id,
        audio_book_id=audio_book_id,
        ebook_book_id=ebook_book_id,
        library_id=library_id,
        audio_folder=str(resolved.folder),
        output_path=str(output_path),
        scan_triggered=scan_triggered,
        confirmed=confirmed,
        read_aloud_sync=read_aloud_sync,
        build=build,
    )


def remove_readalong_epub(
    bookorbit_client: "BookOrbitClient", audio_book_id: object, output_path: "Path | str",
) -> bool:
    """Remove a previously-delivered read-along EPUB, in BookOrbit and on disk.

    Looks up ``output_path``'s filename in the audio entry's current file
    list (forcing a fresh read, since a stale cached listing could miss a
    file added since the last detail fetch) and, if BookOrbit knows about it,
    deletes just that file row via ``DELETE /api/v1/books/files/{fileId}``
    (never the whole entry -- the entry's real audio tracks, cover, etc. must
    survive). Always also removes the local file if ``output_path`` is an
    absolute path and it still exists (a bare filename means the delivery was
    an upload, so there is no local copy to look for),
    independent of whether BookOrbit's own delete succeeded, so a caller is
    never left with orphaned bytes on disk because of a BookOrbit-side
    failure.

    Returns ``True`` only if both the BookOrbit-side file row (when found)
    and the on-disk file are gone afterward.
    """
    output_path = Path(output_path)
    filename = output_path.name

    detail = bookorbit_client.get_book_detail(audio_book_id, force=True)
    file_id = None
    for f in (detail or {}).get("files") or []:
        if isinstance(f, dict) and f.get("filename") == filename:
            file_id = f.get("id")
            break

    bookorbit_ok = True
    if file_id is not None:
        bookorbit_ok = bookorbit_client.delete_book_file(file_id)
        if not bookorbit_ok:
            logger.warning(
                "Read-along cleanup: BookOrbit refused to delete file %s ('%s') from "
                "entry %s", file_id, filename, audio_book_id,
            )

    disk_ok = True
    if output_path.is_absolute() and output_path.exists():
        try:
            output_path.unlink()
        except OSError as e:
            logger.error(
                "Read-along cleanup: could not remove '%s' from disk: %s",
                output_path, e, exc_info=True,
            )
            disk_ok = False

    return bookorbit_ok and disk_ok
