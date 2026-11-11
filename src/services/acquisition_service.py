"""Follows a reader's BookOrbit and Shelfmark requests to arrival and pairs each arrival with the book they already had."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import PurePath
from typing import Any, Optional

from src.api.bookorbit_client import BookOrbitClient
from src.utils.time_utils import utcnow

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(seconds=30)
ARRIVED_REFRESH_INTERVAL = timedelta(minutes=10)
GIVE_UP_AFTER = timedelta(days=30)
STALLED_AFTER = timedelta(minutes=30)
OPEN_STATUSES = ("waiting", "in_progress", "needs_look")
STATUS_MAP = {
    "pending": "waiting",
    "approved": "in_progress",
    "searching": "in_progress",
    "grabbed": "in_progress",
    "downloading": "in_progress",
    "importing": "in_progress",
    "needs_review": "needs_look",
    "available": "arrived",
    "rejected": "declined",
    "failed": "failed",
    "cancelled": "cancelled",
}
SHELFMARK_STATUS_MAP = {
    "pending": "waiting",
    "rejected": "declined",
    "queued": "in_progress",
    "resolving": "in_progress",
    "locating": "in_progress",
    "downloading": "in_progress",
    "complete": "arrived",
    "error": "failed",
    "cancelled": "cancelled",
}
_ARRIVED_REMOTE = ("available", "complete")
# A task that is still running decides a Shelfmark request's state, else a finished one, else a failed one.
_SHELFMARK_TASK_PRECEDENCE = {"queued": 3, "resolving": 3, "locating": 3, "downloading": 3, "complete": 2, "error": 1, "cancelled": 1}
_DOWNLOADED = "Downloaded, waiting for the library"
_AUDIO_SOURCES = {"abs": "ABS", "bookorbit": "BookOrbit", "booklore": "BookLore"}
_UNIDENTIFIED = "The arrived book could not be identified yet."


def is_stalled(row: dict) -> bool:
    """Whether an in-progress download has shown no progress for STALLED_AFTER."""
    changed = row.get("progress_changed_at")
    return bool(
        row.get("status") == "in_progress" and row.get("progress_mark") and changed
        and utcnow() - changed >= STALLED_AFTER
    )


def _audio_source(name: Optional[str]) -> Optional[str]:
    """Canonical audio source key for a search-result source label, or None when unsupported."""
    return _AUDIO_SOURCES.get(str(name or "").strip().lower())


def _author_set(value: Any) -> frozenset:
    """Normalized author names from a display string ("A, B") or a list."""
    parts = value if isinstance(value, list) else str(value or "").split(",")
    names = (BookOrbitClient._normalize_string(str(part)) for part in parts)
    return frozenset(name for name in names if name)


class AcquisitionService:
    """Refreshes a reader's BookOrbit and Shelfmark requests and pairs arrivals with the book they were asked for."""

    def __init__(self, database_service, book_mapping_service, shelf_watch_service=None) -> None:
        """``shelf_watch_service`` is the BookOrbit watcher, reused to hand uncertain arrivals to Discover's review."""
        self.database_service = database_service
        self.book_mapping_service = book_mapping_service
        self.shelf_watch_service = shelf_watch_service

    def has_work(self, user_id: int) -> bool:
        """Whether the user has any request that needs a check (one cheap query, no HTTP)."""
        return bool(self._refreshable(user_id))

    def _refreshable(self, user_id: int) -> list[dict]:
        return self.database_service.get_refreshable_acquisition_requests(user_id, utcnow() - GIVE_UP_AFTER)

    def refresh_user(self, user_id: int, client: Any, shelfmark: Any = None) -> list[dict]:
        """Refresh the user's due requests (each at most once per 30 s) and return all their rows.

        ``client`` is the reader's BookOrbit client (it also finds Shelfmark arrivals in the
        library); ``shelfmark`` is their Shelfmark client.
        """
        now = utcnow()
        due = [
            row for row in self._refreshable(user_id)
            if row["last_checked_at"] is None or now - row["last_checked_at"] >= (
                ARRIVED_REFRESH_INTERVAL if row["remote_status"] in _ARRIVED_REMOTE else REFRESH_INTERVAL
            )
        ]
        shelfmark_rows = [row for row in due if row["service"] == "Shelfmark"]
        due = [row for row in due if row["service"] == "BookOrbit"]
        if shelfmark is not None and shelfmark_rows:
            self._refresh_shelfmark(user_id, shelfmark_rows, shelfmark, client)
        if client is not None:
            resolved: dict[int, dict] = {}
            for row in due:
                try:
                    self._refresh_row(user_id, row, client, resolved)
                except Exception:
                    logger.warning("📥 Acquisition: user %s request %s could not be refreshed", user_id, row["id"], exc_info=True)
            try:
                self._pair_groups(user_id, due, resolved, client)
            except Exception:
                logger.warning("📥 Acquisition: user %s could not pair a Both request", user_id, exc_info=True)
        return self.database_service.get_acquisition_requests(user_id)

    def _update(self, user_id: int, row: dict, **changes: Any) -> None:
        self.database_service.update_acquisition_request(user_id, row["id"], **changes)

    def _gave_up(self, user_id: int, row: dict) -> bool:
        """Fail a request that has been open for 30 days; True when it did."""
        if row["status"] not in OPEN_STATUSES or utcnow() - row["created_at"] < GIVE_UP_AFTER:
            return False
        logger.info("📥 Acquisition: user %s request %s gave up after 30 days (no longer tracked)", user_id, row["id"])
        self._update(user_id, row, status="failed", status_detail="no longer tracked", last_checked_at=utcnow())
        return True

    def _refresh_row(self, user_id: int, row: dict, client: Any, resolved: dict) -> None:
        changes: dict[str, Any] = {"last_checked_at": utcnow()}
        if self._gave_up(user_id, row):
            return
        remote = client.get_request(row["remote_id"]) or {}
        status = remote.get("status")
        if status:
            changes.update(self.remote_changes(row, remote))
            if status == "available":
                changes.update(self._on_available(user_id, row, remote, client, resolved))
            if changes["status"] != row["status"]:
                logger.info(
                    "📥 Acquisition: user %s request %s moved %s -> %s (BookOrbit status %s)",
                    user_id, row["id"], row["status"], changes["status"], status,
                )
        self._update(user_id, row, **changes)

    @classmethod
    def remote_changes(cls, row: dict, remote: dict) -> dict:
        """Row changes for a BookOrbit request object: status, detail and the download-progress mark."""
        status = remote.get("status")
        mapped = STATUS_MAP.get(status, "in_progress")
        download = remote.get("download") if isinstance(remote.get("download"), dict) else {}
        value = download.get("downloadedBytes")
        value = download.get("progressPercent") if value is None else value
        mark = f"{download.get('id')}|{download.get('grabbedAt')}|{value}" if status in ("grabbed", "downloading") and download else None
        return {
            "status": mapped, "remote_status": status, "status_detail": cls._detail(remote, mapped),
            **cls._progress_changes(row, mark),
        }

    @staticmethod
    def _progress_changes(row: dict, mark: Optional[str]) -> dict:
        """Stamp when a download's progress mark last changed; clear both once it is not downloading.

        A polling timestamp is no evidence of progress, so only a different mark moves the stamp.
        """
        if mark is None:
            return {"progress_mark": None, "progress_changed_at": None}
        mark = mark[:64]
        if row.get("progress_mark") == mark and row.get("progress_changed_at") is not None:
            return {}
        return {"progress_mark": mark, "progress_changed_at": utcnow()}

    @staticmethod
    def _detail(remote: dict, mapped: str) -> Optional[str]:
        """BookOrbit's own reason text, else the download percentage while it downloads."""
        detail = remote.get("statusReason") or remote.get("failureCode")
        if detail or mapped != "in_progress" or remote.get("status") != "downloading":
            return detail
        percent = (remote.get("download") or {}).get("progressPercent")
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            return f"{round(percent)}%"
        return None

    # ---- Shelfmark -----------------------------------------------------------

    def _refresh_shelfmark(self, user_id: int, rows: list[dict], shelfmark: Any, bookorbit: Any) -> None:
        """Bring the due Shelfmark rows up to date from one activity read; never raises."""
        activity, error = shelfmark.get_activity()
        if activity is None:
            logger.debug("📥 Acquisition: user %s Shelfmark could not be checked (%s)", user_id, error)
            for row in rows:
                self._update(user_id, row, last_checked_at=utcnow())
            return
        for row in rows:
            try:
                if not self._gave_up(user_id, row):
                    self._refresh_shelfmark_row(user_id, row, activity, bookorbit)
            except Exception:
                logger.warning("📥 Acquisition: user %s request %s could not be refreshed", user_id, row["id"], exc_info=True)

    @staticmethod
    def _shelfmark_remote(row: dict, activity: dict) -> Optional[tuple[str, dict, dict]]:
        """(status, download task, request) Shelfmark reports for a row, or None when it lists neither.

        A task's status is the name of the activity bucket it sits in.
        """
        tasks = [
            (state, key, payload) for state, bucket in (activity.get("status") or {}).items() if isinstance(bucket, dict)
            for key, payload in bucket.items() if isinstance(payload, dict) and state in _SHELFMARK_TASK_PRECEDENCE
        ]
        remote_id = row["remote_id"]
        if row["remote_kind"] != "request":
            for state, key, payload in tasks:
                if key == remote_id or str(payload.get("id")) == remote_id:
                    return state, payload, {}
            return None
        request = next((r for r in activity.get("requests") or [] if str(r.get("id")) == remote_id), None)
        if request is None:
            return None
        if request.get("status") != "fulfilled":
            return str(request.get("status")), {}, request
        linked = [(state, payload) for state, _, payload in tasks if str(payload.get("request_id")) == remote_id]
        state, task = max(linked, key=lambda item: _SHELFMARK_TASK_PRECEDENCE[item[0]], default=(None, {}))
        delivery = request.get("delivery_state")
        return state or (delivery if delivery in _SHELFMARK_TASK_PRECEDENCE else "queued"), task, request

    def _refresh_shelfmark_row(self, user_id: int, row: dict, activity: dict, bookorbit: Any) -> None:
        changes: dict[str, Any] = {"last_checked_at": utcnow()}
        remote = self._shelfmark_remote(row, activity)
        if remote is not None:
            status, task, request = remote
            mapped = SHELFMARK_STATUS_MAP.get(status, "in_progress")
            progress = task.get("progress")
            detail = {
                "error": task.get("status_message") or request.get("last_failure_reason"),
                "rejected": request.get("admin_note"),
                "downloading": f"{round(progress)}%" if isinstance(progress, (int, float)) and not isinstance(progress, bool) else None,
            }.get(status)
            mark = str(progress) if status == "downloading" and isinstance(progress, (int, float)) and not isinstance(progress, bool) else None
            changes.update(status=mapped, remote_status=status, status_detail=detail, **self._progress_changes(row, mark))
            if status == "complete":
                changes.update(self._on_downloaded(user_id, row, task, bookorbit))
            if changes["status"] != row["status"]:
                logger.info(
                    "📥 Acquisition: user %s request %s moved %s -> %s (Shelfmark status %s)",
                    user_id, row["id"], row["status"], changes["status"], status,
                )
        self._update(user_id, row, **changes)

    def _on_downloaded(self, user_id: int, row: dict, task: dict, bookorbit: Any) -> dict:
        """Changes for a finished download: note the file, then find the book in the reader's BookOrbit."""
        filename = PurePath(str(task.get("download_path") or "")).name or row["arrived_filename"]
        changes: dict[str, Any] = {"arrived_filename": filename} if filename else {}
        if row["status"] == "needs_look" and row["remote_status"] == "complete":
            return {**changes, **self._after_review(user_id, row)}
        changes["status_detail"] = _DOWNLOADED
        if bookorbit is None:
            return changes
        book = bookorbit.find_book_by_filename(filename) if row["media_kind"] == "ebook" and filename else None
        if book and book.get("id") is not None:
            arrival, how, candidates = {"id": str(book["id"]), "filename": filename}, "file name", []
        else:
            arrival, how, candidates = self._resolve_arrival(bookorbit, row, {})
        if arrival is None:
            if candidates and row["partner_source_id"]:
                changes.update(self._unresolved(user_id, row, bookorbit, candidates))
            return changes
        logger.info("📥 Acquisition: user %s request %s found in BookOrbit as %s (via %s)", user_id, row["id"], arrival["id"], how)
        changes.update(arrived_source="BookOrbit", arrived_source_id=arrival["id"], status_detail=None)
        if arrival["filename"]:
            changes["arrived_filename"] = arrival["filename"]
        if row["partner_source_id"]:
            changes.update(self._pair_with_partner(user_id, row, arrival, how, bookorbit))
        return changes

    # ---- arrival -------------------------------------------------------------

    def _on_available(self, user_id: int, row: dict, remote: dict, client: Any, resolved: dict) -> dict:
        """Changes for a request BookOrbit says is available: identify the arrival, then pair it."""
        if row["status"] == "needs_look" and row["remote_status"] == "available":
            return self._after_review(user_id, row)
        if not (row["partner_source_id"] or row["group_key"]):
            matched = remote.get("matchedBookId")
            return {} if matched is None else {"arrived_source": "BookOrbit", "arrived_source_id": str(matched)}
        arrival, how, candidates = self._resolve_arrival(client, row, remote)
        if arrival is None:
            return self._unresolved(user_id, row, client, candidates)
        changes: dict[str, Any] = {"arrived_source": "BookOrbit", "arrived_source_id": arrival["id"]}
        if arrival["filename"]:
            changes["arrived_filename"] = arrival["filename"]
        if row["partner_source_id"]:
            return {**changes, **self._pair_with_partner(user_id, row, arrival, how, client)}
        resolved[row["id"]] = {"arrival": arrival, "how": how}
        return changes

    def _resolve_arrival(self, client: Any, row: dict, remote: dict) -> tuple[Optional[dict], str, list]:
        """Find which BookOrbit book arrived: (arrival, how, candidates); arrival is None when uncertain."""
        kind = row["media_kind"]
        note = ""
        matched = remote.get("matchedBookId")
        if matched is not None:
            detail = client.get_book_detail(matched, force=True)
            book_file = BookOrbitClient._primary_file(detail, kind=kind) if detail else None
            if book_file:
                return {"id": str(matched), "filename": book_file.get("filename") if kind == "ebook" else None}, "matchedBookId", []
            note = f", matchedBookId {matched} has no {kind} file"
        search = client.search_ebooks if kind == "ebook" else client.search_audiobooks
        hits = [hit for hit in (search(row["title"]) or []) if hit.get("id") is not None]
        title, authors = BookOrbitClient._normalize_string(row["title"]), _author_set(row["authors"])
        exact = [
            hit for hit in hits
            if authors and BookOrbitClient._normalize_string(hit.get("title")) == title
            and _author_set(hit.get("authors")) == authors
        ]
        if len(exact) == 1:
            hit = exact[0]
            return {"id": str(hit["id"]), "filename": hit.get("fileName") if kind == "ebook" else None}, f"exact title and author match{note}", hits
        return None, f"{len(exact)} exact matches{note}", hits

    def _unresolved(self, user_id: int, row: dict, client: Any, candidates: list) -> dict:
        """Send an uncertain arrival to Discover's review, or wait for BookOrbit's library to show it."""
        held = self._partner_book(row)
        if held is not None and (held.audio_source_id if row["media_kind"] == "audiobook" else held.ebook_filename):
            return self._refuse(user_id, row, "the book you asked it for is already paired with another one")
        outcome = self._hand_off(user_id, row, client, candidates) if candidates else None
        if outcome == "review":
            logger.info(
                "📥 Acquisition: user %s request %s sent to Discover review (%d candidate BookOrbit %s books)",
                user_id, row["id"], len(candidates), row["media_kind"],
            )
            return {"status": "needs_look"}
        if isinstance(outcome, dict):
            return outcome
        return self._refuse(user_id, row, _UNIDENTIFIED)

    def _refuse(self, user_id: int, row: dict, reason: str) -> dict:
        """Keep the row arrived with a reason; log it only when the reason changes."""
        if row["status_detail"] != reason:
            logger.info("📥 Acquisition: user %s request %s not paired: %s", user_id, row["id"], reason)
        return {"status": "arrived", "status_detail": reason}

    def _hand_off(self, user_id: int, row: dict, client: Any, candidates: list):
        """Auto-pair a unique top match at the watcher's threshold, else save a review suggestion.

        Returns the row changes when it paired or was refused, "review" when a suggestion was saved,
        and None when there was nothing to scan or suggest.
        """
        shelf = self.shelf_watch_service
        if shelf is None:
            return None
        if row["media_kind"] == "audiobook":
            if str(row["partner_source"]).lower() != "bookorbit" or not row["partner_filename"]:
                return None
            anchors = [(row["partner_filename"], row["title"], row["authors"] or "", row["partner_source_id"])]
        else:
            anchors = [
                (hit.get("fileName"), hit.get("title") or row["title"], hit.get("authors") or "", str(hit["id"]))
                for hit in candidates[:3] if hit.get("fileName")
            ]
        pool = shelf._build_candidate_pool(client)
        if not pool:
            return None
        scanner = shelf._get_suggestions_service()
        for filename, title, authors, source_id in anchors:
            anchor = {"filename": filename, "title": title, "authors": authors, "grimmory_id": source_id, "path": ""}
            scanned = (scanner._scan_single_ebook(anchor, pool) or {}).get("matches") or []
            if row["media_kind"] == "ebook":
                matches = [m for m in scanned if str(m.get("audio_source_id")) == str(row["partner_source_id"])]
                top_fits = bool(scanned) and scanned[0] in matches
            else:
                matches = scanned
                top_fits = bool(scanned) and _audio_source(scanned[0].get("audio_source")) == "BookOrbit"
            if not matches:
                continue
            score = float(scanned[0].get("score") or 0.0)
            if top_fits and score >= shelf._threshold() and shelf._unique_top(scanned, filename, "Acquisition"):
                return self._auto_pair(user_id, row, client, scanned[0], filename, source_id, score)
            if shelf._create_pending_suggestion(
                {"title": title, "authors": authors}, filename, source_id, matches,
                origin="reading_watch", user_id=user_id,
            ):
                return "review"
        return None

    def _auto_pair(self, user_id: int, row: dict, client: Any, winner: dict, ebook_filename: str,
                   ebook_id: str, score: float) -> dict:
        """Pair a scan winner with the partner through the normal refusal checks."""
        if row["media_kind"] == "audiobook":
            arrival_id = str(winner.get("audio_source_id"))
            audio = ("BookOrbit", arrival_id)
            ebook = (row["partner_source"], row["partner_source_id"], row["partner_filename"])
            arrival = {"arrived_source": "BookOrbit", "arrived_source_id": arrival_id}
            other = "ebook"
        else:
            arrival_id = ebook_id
            audio = (_audio_source(row["partner_source"]), row["partner_source_id"])
            ebook = ("BookOrbit", ebook_id, ebook_filename)
            arrival = {"arrived_source": "BookOrbit", "arrived_source_id": ebook_id, "arrived_filename": ebook_filename}
            other = "audiobook"
        abs_id, reason = self._pair_books(user_id, audio, ebook, row["title"], client)
        if abs_id is None:
            return {**arrival, **self._refuse(user_id, row, reason)}
        logger.info(
            "📥 Acquisition: user %s request %s paired BookOrbit %s %s with %s %s (via auto-match score %s)",
            user_id, row["id"], row["media_kind"], arrival_id, other, row["partner_source_id"], score,
        )
        return {**arrival, "status": "paired", "linked_abs_id": abs_id, "status_detail": None}

    def _after_review(self, user_id: int, row: dict) -> dict:
        """A request handed to review stays 'needs_look' until the reader's approval has mapped the pair."""
        held = self._partner_book(row)
        done = held is not None and (held.ebook_filename and held.audio_source_id)
        if not done:
            return {"status": "needs_look"}
        logger.info("📥 Acquisition: user %s request %s paired through Discover review (mapping %s)", user_id, row["id"], held.abs_id)
        return {"status": "paired", "linked_abs_id": held.abs_id}

    # ---- pairing -------------------------------------------------------------

    def _audio_book(self, source: str, source_id: str):
        book = self.database_service.get_book_by_audio_source(source, source_id)
        return book or (self.database_service.get_book(source_id) if source == "ABS" else None)

    def _ebook_book(self, source: Optional[str], source_id: Optional[str], filename: Optional[str]):
        book = self.database_service.get_book_by_ebook_source(source, source_id) if source and source_id else None
        return book or (self.database_service.get_book_by_ebook_filename(filename) if filename else None)

    def _partner_book(self, row: dict):
        """The mapping the reader's existing book (the row's partner) already belongs to, if any."""
        if row["media_kind"] == "audiobook":
            return self._ebook_book(row["partner_source"], row["partner_source_id"], row["partner_filename"])
        source = _audio_source(row["partner_source"])
        return self._audio_book(source, row["partner_source_id"]) if source else None

    def _pair_with_partner(self, user_id: int, row: dict, arrival: dict, how: str, client: Any) -> dict:
        """Pair the arrival with the row's partner; the row's status and link changes."""
        if row["media_kind"] == "audiobook":
            audio = ("BookOrbit", arrival["id"])
            ebook = (row["partner_source"], row["partner_source_id"], row["partner_filename"])
            other = "ebook"
        else:
            audio = (_audio_source(row["partner_source"]), row["partner_source_id"])
            ebook = ("BookOrbit", arrival["id"], arrival["filename"])
            other = "audiobook"
        abs_id, reason = self._pair_books(user_id, audio, ebook, row["title"], client)
        if abs_id is None:
            return self._refuse(user_id, row, reason)
        logger.info(
            "📥 Acquisition: user %s request %s paired BookOrbit %s %s with %s %s (via %s)",
            user_id, row["id"], row["media_kind"], arrival["id"], other, row["partner_source_id"], how,
        )
        return {"status": "paired", "linked_abs_id": abs_id, "status_detail": None}

    def _pair_groups(self, user_id: int, due: list[dict], resolved: dict, client: Any) -> None:
        """Pair the two halves of a Both request with each other once both have been identified."""
        groups: dict[str, dict[str, dict]] = {}
        for row in due:
            if row["group_key"] and row["id"] in resolved:
                groups.setdefault(row["group_key"], {})[row["media_kind"]] = row
        for halves in groups.values():
            if len(halves) != 2:
                continue
            ebook_row, audio_row = halves["ebook"], halves["audiobook"]
            ebook, audio = resolved[ebook_row["id"]], resolved[audio_row["id"]]
            abs_id, reason = self._pair_books(
                user_id, ("BookOrbit", audio["arrival"]["id"]),
                ("BookOrbit", ebook["arrival"]["id"], ebook["arrival"]["filename"]), audio_row["title"], client,
            )
            for row in (ebook_row, audio_row):
                if abs_id is None:
                    self._update(user_id, row, **self._refuse(user_id, row, reason))
                    continue
                logger.info(
                    "📥 Acquisition: user %s request %s paired BookOrbit audiobook %s with ebook %s (both halves of one request)",
                    user_id, row["id"], audio["arrival"]["id"], ebook["arrival"]["id"],
                )
                self._update(user_id, row, status="paired", linked_abs_id=abs_id, status_detail=None)

    def _pair_books(self, user_id: int, audio: tuple, ebook: tuple, title: str, client: Any) -> tuple[Optional[str], str]:
        """Map an audiobook to an ebook; (abs_id, '') on success or when already paired, else (None, reason).

        Never takes over a mapping that already pairs either book with something else. The one merge it
        allows is the one create_audio_mapping_from_match makes safely: an ebook-only mapping of this
        same ebook is folded into the new pairing.
        """
        (audio_source, audio_id), (ebook_source, ebook_id, ebook_filename) = audio, ebook
        if not audio_source or not audio_id or not ebook_id or not ebook_filename:
            return None, "The other book's details are incomplete."
        audio_book = self._audio_book(audio_source, audio_id)
        ebook_book = self._ebook_book(ebook_source, ebook_id, ebook_filename)
        if audio_book and ebook_book and audio_book.abs_id == ebook_book.abs_id:
            return audio_book.abs_id, ""
        if audio_book and audio_book.ebook_filename:
            return None, "That audiobook is already paired with another ebook."
        if ebook_book and ebook_book.audio_source_id:
            return None, "That ebook is already paired with another audiobook."
        info = client.get_audiobook_info(audio_id) if audio_source == "BookOrbit" else None
        saved = self.book_mapping_service.create_audio_mapping_from_match(
            audio_source=audio_source, audio_source_id=str(audio_id), audio_title=title,
            audio_duration=(info or {}).get("duration_seconds"),
            ebook_filename=ebook_filename, ebook_source=ebook_source, ebook_source_id=str(ebook_id),
            user_id=user_id,
        )
        if not saved:
            return None, "The pairing could not be saved."
        if audio_source == "BookOrbit" and self.shelf_watch_service is not None:
            self.shelf_watch_service._request_readalong_for_audio_match(saved)
        return saved.abs_id, ""
