"""Follows a reader's BookOrbit and Shelfmark requests to arrival and pairs each arrival with the book they already had."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from types import SimpleNamespace
from threading import Lock
from typing import Any, Optional

from src.api.bookorbit_client import BookOrbitClient
from src.services.acquisition_library_catalog import search_reader_library
from src.utils.config_loader import env_truthy
from src.utils.time_utils import utcnow
from src.utils.user_config import (
    _ALLOW_GLOBAL_FALLBACK_KEY,
    _TRUTHY_SETTING_VALUES,
    global_fallback_allowed,
    resolve_setting,
)

logger = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(seconds=30)
ARRIVED_REFRESH_INTERVAL = timedelta(minutes=10)
GIVE_UP_AFTER = timedelta(days=30)
JOB_RECONCILE_INTERVAL = REFRESH_INTERVAL
SCAN_HISTORY_GRACE = timedelta(minutes=30)
ADOPTION_SCAN_INTERVAL = timedelta(minutes=10)
ADOPTION_PAGE_SIZE = 100
ADOPTION_MAX_PAGES = 20
ADOPT_SETTING = "BOOKORBIT_ADOPT_REQUESTS"
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
_DEAD_RECEIPT = ("failed", "cancelled", "declined")
# A task that is still running decides a Shelfmark request's state, else a finished one, else a failed one.
_SHELFMARK_TASK_PRECEDENCE = {"queued": 3, "resolving": 3, "locating": 3, "downloading": 3, "complete": 2, "error": 1, "cancelled": 1}
_DOWNLOADED = "Downloaded, waiting for the library"
_AUDIO_SOURCES = {"abs": "ABS", "bookorbit": "BookOrbit", "booklore": "BookLore"}
_UNIDENTIFIED = "The arrived book could not be identified yet."
_AUDIO_PAIRED = "That audiobook is already paired with another ebook."
_NO_EBOOK = "No single matching ebook was found in your library."


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


def _parse_time(value: Any) -> Optional[datetime]:
    """A BookOrbit ISO timestamp as naive UTC, or None when missing or unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _series_key(name: Any, index: Any) -> Optional[tuple]:
    """Comparable series identity, or None when known metadata is incomplete or unreadable."""
    normalized = BookOrbitClient._normalize_string(str(name or ""))
    if not normalized and index in (None, ""):
        return ("", None)
    if not normalized or index in (None, "") or isinstance(index, bool):
        return None
    try:
        position = float(index)
    except (TypeError, ValueError):
        return None
    return (normalized, position) if math.isfinite(position) else None


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
        self._adoption_scanned: dict[int, datetime] = {}
        self._adoption_active: dict[int, bool] = {}
        self._adoption_claim_lock = Lock()
        self._job_reconciled: dict[str, datetime] = {}
        self.job_reconcile_interval = JOB_RECONCILE_INTERVAL

    def _pending_jobs(self, user_id: int) -> list[dict]:
        """Unpaired jobs and outstanding scans; jobs older than ``GIVE_UP_AFTER`` are dropped."""
        return self.database_service.get_unpaired_acquisition_jobs(user_id, utcnow() - GIVE_UP_AFTER)

    def has_work(self, user_id: int) -> bool:
        """Work can exist without any provider receipt (both halves already owned)."""
        pending = getattr(self.database_service, "get_unpaired_acquisition_jobs", None)
        return bool(self._refreshable(user_id)) or (bool(self._pending_jobs(user_id)) if pending else False) or self._adoption_due(user_id)

    def _refreshable(self, user_id: int) -> list[dict]:
        return self.database_service.get_refreshable_acquisition_requests(user_id, utcnow() - GIVE_UP_AFTER)

    def refresh_user(self, user_id: int, client: Any, shelfmark: Any = None,
                     library_clients: Any = None) -> list[dict]:
        """Refresh the user's due requests (each at most once per 30 s) and return all their rows.

        ``library_clients`` is the same reader's authenticated client bundle used
        by Discover. No global fallback is used when the bundle is unavailable.
        """
        # The caller may pass only the reader-scoped library bundle. Use that
        # bundle's own provider client for receipt polling too; never globalize.
        if client is None and library_clients is not None:
            client = getattr(library_clients, "bookorbit_client", None)
            if client is not None and not client.is_configured():
                client = None
        now = utcnow()
        claimed = client is not None and self._claim_adoption(user_id)
        try:
            if claimed:
                try:
                    self._adopt_requests(user_id, client)
                except Exception:
                    logger.warning("📥 Acquisition: user %s BookOrbit requests could not be adopted", user_id, exc_info=True)
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
            if library_clients is None and client is not None:
                library_clients = SimpleNamespace(bookorbit_client=client)
            try:
                self._reconcile_jobs(user_id, library_clients)
            except Exception:
                logger.warning("📥 Acquisition: user %s job reconciliation failed", user_id, exc_info=True)
            return self.database_service.get_acquisition_requests(user_id)
        finally:
            if claimed:
                with self._adoption_claim_lock:
                    self._adoption_active.pop(user_id, None)

    def _is_job_receipt(self, user_id: int, row: dict) -> bool:
        """Distinguish durable job receipts from older group-key-only requests."""
        group_key = row.get("group_key")
        return bool(group_key and hasattr(self.database_service, "get_acquisition_job")
                    and self.database_service.get_acquisition_job(user_id, group_key))

    @staticmethod
    def _candidate_matches(job: dict, candidate: dict, *, explicit: bool = False) -> bool:
        """Require positive edition identity, not just a filename or similar title."""
        norm = BookOrbitClient._normalize_string
        if not candidate.get("source_id") or not candidate.get("source"):
            return False
        if norm(str(candidate.get("title") or "")) != norm(str(job.get("title") or "")):
            return False
        authors = _author_set(job.get("authors"))
        found = _author_set(candidate.get("authors"))
        if authors and ((not found and not explicit) or (found and authors != found)):
            return False
        isbn = str(job.get("isbn13") or "").replace("-", "")
        found_isbn = str(candidate.get("isbn13") or "").replace("-", "")
        if isbn and (found_isbn != isbn and (found_isbn or not explicit)):
            # An unknown catalog ISBN cannot prove which newly found edition
            # fulfils a request with a specific ISBN. A reader-selected stable
            # service+ID is still authoritative when its metadata omits ISBN.
            return False
        if not authors and (not isbn or found_isbn != isbn) and not explicit:
            return False
        return AcquisitionService._series_matches(job, candidate, explicit=explicit)

    @staticmethod
    def _series_matches(job: dict, candidate: dict, *, explicit: bool) -> bool:
        """Match known series parts; an explicit choice may fill missing metadata only."""
        expected_name = BookOrbitClient._normalize_string(str(job.get("series_name") or ""))
        found_name = BookOrbitClient._normalize_string(str(candidate.get("series_name") or ""))
        expected_index = job.get("series_index")
        found_index = candidate.get("series_index")
        expected_has_index = expected_index not in (None, "")
        found_has_index = found_index not in (None, "")

        if not explicit:
            if not expected_name and not expected_has_index:
                return True
            expected = _series_key(job.get("series_name"), expected_index)
            actual = _series_key(candidate.get("series_name"), found_index)
            return expected is not None and expected == actual

        if found_name and expected_name and found_name != expected_name:
            return False
        expected_number = None
        if expected_has_index:
            if isinstance(expected_index, bool):
                return False
            try:
                expected_number = float(expected_index)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(expected_number):
                return False
        if found_has_index:
            if isinstance(found_index, bool):
                return False
            try:
                found_number = float(found_index)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(found_number):
                return False
            if expected_number is not None and expected_number != found_number:
                return False
        return True

    def choose_review_candidate(
        self, user_id: int, job_id: str, media_kind: str, selection: dict, clients: Any,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Confirm one persisted review choice after checking the reader's live catalog."""
        if media_kind not in ("ebook", "audiobook") or not isinstance(selection, dict):
            return None, "invalid_candidate"
        job = self.database_service.get_acquisition_job(user_id, job_id)
        if job is None:
            return None, "not_found"
        if job.get("dismissed") or media_kind not in job.get("formats", []):
            return None, "not_allowed"
        prefix = "ebook" if media_kind == "ebook" else "audio"
        if job.get(f"{prefix}_library_state") != "needs_review":
            return None, "review_not_pending"

        source = str(selection.get("source") or "")
        source_id = str(selection.get("source_id") or "")
        filename = str(selection.get("filename") or "") if media_kind == "ebook" else ""
        persisted = job.get("review_candidates", {}).get(media_kind, [])
        offered = any(
            candidate.get("source") == source
            and str(candidate.get("source_id")) == source_id
            and (media_kind != "ebook" or candidate.get("filename") == filename)
            for candidate in persisted if isinstance(candidate, dict)
        )
        if not offered or not source or not source_id or (media_kind == "ebook" and not filename):
            return None, "candidate_not_offered"

        candidates, errors = search_reader_library(clients, media_kind, job["title"])
        if source in errors:
            return None, "catalog_unavailable"
        matches = [candidate for candidate in candidates if (
            candidate.get("source") == source
            and str(candidate.get("source_id")) == source_id
            and (media_kind != "ebook" or candidate.get("filename") == filename)
        )]
        if len(matches) != 1:
            return None, "candidate_not_current"
        selected = matches[0]
        if not self._candidate_matches(job, selected, explicit=True):
            return None, "candidate_metadata_conflict"

        identity = {"source": selected["source"], "source_id": str(selected["source_id"])}
        if media_kind == "ebook":
            identity["filename"] = selected["filename"]
        # The reader's choice is the evidence that attributes a completed receipt
        # the provider gave no library handle for; otherwise it would be re-flagged.
        unattributed = [r["id"] for r in job["requests"] if (
            r["media_kind"] == media_kind and r["status"] == "arrived"
            and r["remote_status"] in _ARRIVED_REMOTE and not r.get("arrived_source_id")
        )]
        saved = None
        try:
            for receipt_id in unattributed or [None]:
                saved = self.database_service.confirm_acquisition_arrival(
                    user_id, job_id, media_kind, identity, receipt_id=receipt_id,
                )
        except ValueError:
            return None, "identity_conflict"
        return (saved, None) if saved else (None, "not_found")

    def retry_acquisition_scan(self, user_id: int, job_id: str, clients: Any) -> tuple[dict, Optional[str]]:
        """Retry only a settled/refused scan after validating its reader scope and history."""
        job = self.database_service.get_acquisition_job(user_id, job_id)
        if job is None:
            return {}, "not_found"
        if job.get("dismissed"):
            return {}, "job_dismissed"
        if not job.get("audio_delivered_at") or not any(
            row.get("service") == "Shelfmark" and row.get("media_kind") == "audiobook"
            and row.get("remote_status") == "complete" for row in job.get("requests", [])
        ):
            return job, "scan_not_ready"
        if job.get("audio_library_state") == "indexed" or job.get("linked_abs_id"):
            return job, "audiobook_already_indexed"

        state = job.get("scan_state")
        if state not in ("failed", "completed", "refused", "invalid", "unavailable", "conflict"):
            return job, "scan_not_retryable"
        if (job.get("scan_attempts") or 0) >= 2:
            return job, "scan_retry_limit_reached"
        if state in ("failed", "completed") and not job.get("scan_job_id"):
            return job, "scan_operation_unverified"
        if state in ("refused", "invalid", "unavailable", "conflict") and job.get("scan_job_id"):
            return job, "scan_operation_unverified"

        bookorbit = getattr(clients, "bookorbit_client", None) if clients is not None else None
        if bookorbit is None or not bookorbit.is_configured():
            return job, "reader_bookorbit_unavailable"
        scope_reader = getattr(bookorbit, "get_scan_scope", None)
        scope_value = scope_reader() if callable(scope_reader) else ""
        scope = scope_value if isinstance(scope_value, str) and scope_value else None
        if not scope:
            return job, "scan_server_scope_unavailable"
        if job.get("scan_scope") and job["scan_scope"] != scope:
            return job, "scan_server_scope_changed"
        if job.get("scan_job_id") and not job.get("scan_scope"):
            return job, "scan_server_scope_unverified"

        configured_destination = self._scan_target(user_id)
        destination = job.get("scan_library_id") or configured_destination
        if not destination or not str(destination).isdigit() or int(destination) <= 0:
            return job, "invalid_scan_library_id"
        destination = str(destination)
        if job.get("scan_library_id") and configured_destination != destination:
            return job, "scan_destination_changed"
        try:
            libraries = bookorbit.get_libraries()
            if not any(isinstance(item, dict) and str(item.get("id")) == destination for item in libraries):
                return job, "scan_library_missing_or_inaccessible"
            history, error = bookorbit.get_scan_history(destination)
        except Exception:
            logger.warning("📥 Acquisition: could not verify scan retry for job %s", job_id, exc_info=True)
            return job, "scan_history_unavailable"
        if history is None:
            return job, error or "scan_history_unavailable"

        history_entry = None
        if job.get("scan_job_id"):
            history_entry = next((entry for entry in history if (
                isinstance(entry, dict) and str(entry.get("id")) == str(job["scan_job_id"])
            )), None)
            if history_entry is None:
                return job, "scan_operation_not_in_history"
            status = str(history_entry.get("status") or "").lower()
            settled = ({"failed", "error", "cancelled"} if state == "failed"
                       else {"completed", "complete", "success"})
            if status not in settled:
                return job, "scan_operation_not_settled"
        if any(str(entry.get("status") or "").lower() in ("running", "queued")
               for entry in history if isinstance(entry, dict)):
            return job, "scan_already_running"

        claim = self.database_service.claim_acquisition_scan_retry(
            user_id, job_id, destination, scope,
            expected_state=state,
            expected_job_id=job.get("scan_job_id"),
            expected_library_id=job.get("scan_library_id"),
            expected_scan_scope=job.get("scan_scope"),
        )
        if claim.get("state") != "claimed":
            return job, claim.get("state", "scan_not_retryable")
        try:
            result = bookorbit.start_library_scan_tracked(destination)
        except Exception:
            logger.warning("📥 Acquisition scan retry acknowledgement is uncertain for job %s", job_id, exc_info=True)
            result = {"state": "uncertain", "reason": "scan_acknowledgement_missing", "job_id": None}
        self.database_service.update_acquisition_scan(
            user_id, job_id, result["state"],
            provider_job_id=result.get("job_id"), reason=result.get("reason"),
        )
        updated = self.database_service.get_acquisition_job(user_id, job_id) or job
        return updated, (None if result.get("state") == "accepted" else result.get("reason") or result.get("state"))

    @staticmethod
    def _candidate_reviewable(job: dict, candidate: dict) -> bool:
        """Keep plausible same-title editions for reader review when proof is incomplete."""
        norm = BookOrbitClient._normalize_string
        if (not candidate.get("source") or not candidate.get("source_id")
                or norm(str(candidate.get("title") or "")) != norm(str(job.get("title") or ""))):
            return False
        expected_authors, found_authors = _author_set(job.get("authors")), _author_set(candidate.get("authors"))
        if expected_authors and found_authors and expected_authors != found_authors:
            return False
        expected_isbn = str(job.get("isbn13") or "").replace("-", "")
        found_isbn = str(candidate.get("isbn13") or "").replace("-", "")
        if expected_isbn and found_isbn and expected_isbn != found_isbn:
            return False
        return AcquisitionService._series_matches(job, candidate, explicit=True)

    def _reconcile_jobs(self, user_id: int, clients: Any) -> None:
        """Reconcile each saved half independent of receipt timing and page lifetime.

        The authenticated per-reader catalog is queried again before pairing;
        a saved arrival is never sufficient evidence if its library disappears.
        """
        if getattr(self.database_service, "get_unpaired_acquisition_jobs", None) is None:
            return
        now = utcnow()
        jobs = self._pending_jobs(user_id)
        for job in jobs:
            requests = job.get("requests") or []
            if requests and all(r["status"] in _DEAD_RECEIPT for r in requests) and not job.get("scan_state"):
                continue  # every provider request ended without a delivery; nothing can arrive
            last = self._job_reconciled.get(job["id"])
            if last is not None and now - last < self.job_reconcile_interval:
                continue
            self._job_reconciled[job["id"]] = now
            try:
                self._reconcile_job(user_id, job, clients)
            except Exception:
                logger.warning("📥 Acquisition: user %s job %s could not reconcile", user_id, job["id"], exc_info=True)
        live = {job["id"] for job in jobs}
        for job_id in [key for key in self._job_reconciled if key not in live]:
            self._job_reconciled.pop(job_id, None)

    def _reconcile_linked_receipts(self, user_id: int, job: dict, clients: Any) -> None:
        """Retain late provider arrivals without inventing their library provenance.

        A title/author/edition match against an existing library item proves
        that item exists, not that the provider's newly completed download
        produced that item. Keep the receipt unpaired until its actual library
        identity is confirmed independently or the reader reviews it.
        """
        if not self.database_service.is_user_linked(user_id, job["linked_abs_id"]):
            return
        for kind in ("ebook", "audiobook"):
            late = [r for r in job["requests"] if (
                r["media_kind"] == kind and r["status"] == "arrived"
                and r["remote_status"] in _ARRIVED_REMOTE
                and not r.get("arrived_source_id") and not r.get("linked_abs_id")
            )]
            if not late:
                continue
            review_reason = "Late provider completion does not identify which library copy arrived"
            prefix = "ebook" if kind == "ebook" else "audio"
            if (job.get(f"{prefix}_library_state") == "needs_review"
                    and job.get(f"{prefix}_library_reason") == review_reason):
                # The same unattributed receipt remains pending reader review;
                # don't rescan all catalogs on every background tick.
                continue
            candidates, _errors = search_reader_library(clients, kind, job["title"])
            eligible = [
                hit for hit in candidates
                if self._candidate_matches(job, hit, explicit=True)
            ]
            self.database_service.set_acquisition_library_state(
                user_id, job["id"], kind, "needs_review",
                review_reason,
                candidates=eligible,
            )

    def _reconcile_job(self, user_id: int, job: dict, clients: Any) -> None:
        if job.get("linked_abs_id"):
            # A finished mapping can still have an in-flight provider request.
            self._reconcile_linked_receipts(user_id, job, clients)
            self._reconcile_audio_scan(user_id, job, clients, already_indexed=True)
            return
        verified: dict[str, dict] = {}
        for kind in ("ebook", "audiobook"):
            prefix = "ebook" if kind == "ebook" else "audio"
            known_source = job.get(f"{prefix}_source")
            known_id = job.get(f"{prefix}_source_id")
            receipts = [r for r in job["requests"] if r["media_kind"] == kind]
            confirmed = [r for r in receipts if r["remote_status"] in _ARRIVED_REMOTE
                         and r["status"] in ("arrived", "paired", "needs_look")]
            # A reader-selected half needs revalidation just like a provider arrival.
            candidates, errors = search_reader_library(clients, kind, job["title"])
            eligible = []
            for hit in candidates:
                exact_saved = bool(known_source and known_id
                                   and hit.get("source") == known_source
                                   and hit.get("source_id") == str(known_id))
                exact_receipt = any(
                    receipt.get("arrived_source") == hit.get("source")
                    and receipt.get("arrived_source_id") == hit.get("source_id")
                    and (kind != "ebook" or receipt.get("arrived_filename") == hit.get("filename"))
                    for receipt in confirmed
                )
                if self._candidate_matches(job, hit, explicit=exact_saved or exact_receipt):
                    eligible.append(hit)
            reviewable = [hit for hit in candidates if self._candidate_reviewable(job, hit)]
            if known_source and known_id:
                matching = [
                    hit for hit in eligible
                    if hit["source"] == known_source and hit["source_id"] == str(known_id)
                    and (kind != "ebook" or hit.get("filename") == job.get("ebook_filename"))
                ]
                if len(matching) != 1:
                    reviewable_saved = [hit for hit in reviewable if (
                        hit.get("source") == known_source and hit.get("source_id") == str(known_id)
                    )]
                    if reviewable_saved:
                        self.database_service.set_acquisition_library_state(
                            user_id, job["id"], kind, "needs_review",
                            "Saved edition is present but current metadata needs review",
                            candidates=reviewable_saved,
                        )
                        continue
                    state = "catalog_unavailable" if known_source in errors or clients is None else "waiting_import"
                    self.database_service.set_acquisition_library_state(
                        user_id, job["id"], kind, state, "Saved edition not currently verified in its library",
                    )
                    continue
                selected = matching[0]
            else:
                if errors:
                    if not eligible and reviewable:
                        self.database_service.set_acquisition_library_state(
                            user_id, job["id"], kind, "needs_review",
                            "Matching library copy lacks enough current metadata for automatic confirmation",
                            candidates=reviewable,
                        )
                        continue
                    self.database_service.set_acquisition_library_state(
                        user_id, job["id"], kind, "catalog_unavailable",
                        "A configured library could not be checked; edition uniqueness is unverified",
                    )
                    continue
                if len(eligible) != 1:
                    needs_review = bool(eligible) or bool(reviewable)
                    state = "needs_review" if needs_review else "waiting_import"
                    reason = (
                        f"{len(eligible)} matching editions; a reader must choose" if eligible
                        else "Matching library copy lacks enough edition metadata for automatic confirmation"
                        if reviewable else "Waiting for library import"
                    )
                    self.database_service.set_acquisition_library_state(
                        user_id, job["id"], kind, state, reason,
                        candidates=eligible or reviewable,
                    )
                    continue
                selected = eligible[0]
            identity = {
                "source": selected["source"], "source_id": selected["source_id"],
            }
            if kind == "ebook":
                identity["filename"] = selected["filename"]
            # Receipt provenance survives. A request may still be in progress
            # even when a library already contains the reader's requested copy.
            # A provider's completion alone does not prove that its delivered
            # file is this already-owned library copy. Only attach positive
            # service+ID evidence recorded from the provider's direct handle.
            receipt = next((r for r in confirmed if (
                r["arrived_source_id"] == selected["source_id"]
                and r["arrived_source"] == selected["source"]
                and (kind != "ebook" or r["arrived_filename"] == selected["filename"])
            )), None)
            try:
                self.database_service.confirm_acquisition_arrival(
                    user_id, job["id"], kind, identity,
                    receipt_id=receipt["id"] if receipt else None,
                )
            except ValueError:
                self.database_service.set_acquisition_library_state(
                    user_id, job["id"], kind, "needs_review",
                    "The arrival conflicts with a previously selected edition",
                    candidates=eligible,
                )
                continue
            verified[kind] = selected

        # Scans are for confirmed Shelfmark AUDIOBOOK deliveries only, never ebooks.
        self._reconcile_audio_scan(user_id, job, clients, already_indexed="audiobook" in verified)
        if "ebook" not in verified or "audiobook" not in verified:
            return
        audio = verified["audiobook"]
        ebook = verified["ebook"]
        client = getattr(clients, "bookorbit_client", None) if clients is not None else None
        abs_id, reason = self._pair_books(
            user_id,
            (audio["source"], audio["source_id"]),
            (ebook["source"], ebook["source_id"], ebook["filename"]),
            job["title"], client,
        )
        if abs_id and not self.database_service.is_user_linked(user_id, abs_id):
            # A successful mapping is not complete for this reader until linked.
            self.database_service.link_user_book(user_id, abs_id)
        self.database_service.set_acquisition_pairing_result(
            user_id, job["id"], abs_id=abs_id,
            reason=reason if not abs_id else None,
        )

    def _scan_target(self, user_id: int) -> str:
        """A live per-reader setting; blank is never borrowed from an admin."""
        return str(self.database_service.get_user_credential(
            user_id, "SHELFMARK_AUDIOBOOK_LIBRARY_ID", "",
        ) or "").strip()

    def _reconcile_audio_scan(self, user_id: int, job: dict, clients: Any, *,
                              already_indexed: bool) -> None:
        """Track scans through job-history verification, not through POST acceptance."""
        receipts = [r for r in job["requests"] if (
            r["service"] == "Shelfmark" and r["media_kind"] == "audiobook"
            and r["remote_status"] == "complete"
        )]
        if not receipts:
            return
        if job.get("audio_delivered_at") is None:
            self.database_service.set_acquisition_delivery_seen(user_id, job["id"])
            job = self.database_service.get_acquisition_job(user_id, job["id"])
        state = job.get("scan_state")
        if already_indexed and state not in ("claimed", "accepted", "coalesced", "waiting_scope", "uncertain"):
            if state == "completed" and job.get("scan_reason") != "audiobook_indexed":
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], "completed", reason="audiobook_indexed",
                )
            elif not state:
                self.database_service.record_acquisition_scan_wait(
                    user_id, job["id"], "scheduled", "audiobook_already_indexed",
                )
            return
        destination = job.get("scan_library_id") if state in (
            "claimed", "accepted", "coalesced", "waiting_scope", "uncertain",
        ) else self._scan_target(user_id)
        if not destination:
            self.database_service.record_acquisition_scan_wait(
                user_id, job["id"], "scheduled", "waiting_for_scheduled_scan",
            )
            return
        if not destination.isdigit() or int(destination) <= 0:
            self.database_service.record_acquisition_scan_wait(
                user_id, job["id"], "invalid", "invalid_scan_library_id",
            )
            return
        bookorbit = getattr(clients, "bookorbit_client", None) if clients is not None else None
        if bookorbit is None or not bookorbit.is_configured():
            if state in ("claimed", "accepted", "coalesced", "waiting_scope", "uncertain"):
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], state, reason="reader_bookorbit_unavailable",
                )
            else:
                self.database_service.record_acquisition_scan_wait(
                    user_id, job["id"], "unavailable", "reader_bookorbit_unavailable",
                )
            return
        scope_reader = getattr(bookorbit, "get_scan_scope", None)
        scope_value = scope_reader() if callable(scope_reader) else ""
        scan_scope = scope_value if isinstance(scope_value, str) and scope_value else None
        if state in ("claimed", "accepted", "coalesced", "waiting_scope", "uncertain") and job.get("scan_scope"):
            if scan_scope != job["scan_scope"]:
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], state,
                    reason="scan_server_scope_changed" if scan_scope else "scan_server_scope_unavailable",
                )
                return
        history, error = bookorbit.get_scan_history(destination)
        if history is None:
            self.database_service.record_acquisition_scan_wait(
                user_id, job["id"], "refused" if error in ("scan_forbidden", "library_unavailable") else "unavailable",
                error or "scan_history_unavailable",
            )
            if state in ("accepted", "coalesced"):
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], state, reason=error or "scan_tracking_unavailable",
                )
            return
        if state in ("claimed", "accepted", "coalesced", "waiting_scope", "uncertain"):
            job_id = str(job.get("scan_job_id") or "")
            entry = next((r for r in history if str(r.get("id")) == job_id), None) if job_id else None
            if state == "waiting_scope" and not job_id:
                running = [r for r in history if r.get("status") in ("running", "queued")]
                if len(running) == 1:
                    self.database_service.update_acquisition_scan(
                        user_id, job["id"], "coalesced", provider_job_id=str(running[0]["id"]),
                        reason="sharing_existing_scan",
                    )
                return
            if state in ("claimed", "uncertain") and not job_id:
                # A crash or malformed POST answer may have started a scan.
                # Never manufacture a job ID from an unrelated history entry.
                return
            if entry is None:
                claimed_at = job.get("scan_claimed_at")
                if state in ("accepted", "coalesced") and claimed_at and utcnow() - claimed_at > SCAN_HISTORY_GRACE:
                    # Long gone from the recent-history window: it settled unseen. Treat it as
                    # finished so the bounded automatic rescan can run instead of waiting forever.
                    self.database_service.update_acquisition_scan(
                        user_id, job["id"], "completed", reason="scan_aged_out_of_history",
                    )
                else:
                    self.database_service.update_acquisition_scan(
                        user_id, job["id"], state, reason="scan_job_not_in_recent_history",
                    )
                return
            status = str(entry.get("status") or "").lower()
            if status in ("completed", "complete", "success"):
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], "completed", reason="scan_finished_catalog_not_yet_indexed",
                )
            elif status in ("failed", "error", "cancelled"):
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], "failed", reason="scan_failed_or_cancelled",
                )
            return
        if state in ("failed", "uncertain", "conflict"):
            # Ambiguous acknowledgement or provider operation: never auto retry.
            return
        if state in ("refused", "invalid") and job.get("scan_library_id") == destination:
            # Same failed configuration needs a user action, not another POST.
            return
        if state == "completed" and (job.get("scan_attempts") or 0) >= 2:
            return
        libraries = bookorbit.get_libraries()
        if not any(isinstance(entry, dict) and str(entry.get("id")) == destination for entry in libraries):
            self.database_service.record_acquisition_scan_wait(
                user_id, job["id"], "refused", "scan_library_missing_or_inaccessible",
            )
            return
        running = [r for r in history if str(r.get("status") or "").lower() in ("running", "queued")]
        if len(running) == 1:
            claim = self.database_service.claim_acquisition_scan(
                user_id, job["id"], destination, scan_scope=scan_scope,
                coalesce_job_id=str(running[0]["id"]),
            )
            logger.info("📥 Acquisition scan %s coalesced as %s", job["id"], claim.get("state"))
            return
        claim = self.database_service.claim_acquisition_scan(
            user_id, job["id"], destination, scan_scope=scan_scope,
        )
        if claim.get("state") != "claimed":
            return
        result = bookorbit.start_library_scan_tracked(destination)
        if result["state"] == "conflict":
            latest, history_error = bookorbit.get_scan_history(destination)
            active = [r for r in (latest or []) if str(r.get("status") or "").lower() in ("running", "queued")]
            if not history_error and len(active) == 1:
                self.database_service.update_acquisition_scan(
                    user_id, job["id"], "coalesced", provider_job_id=str(active[0]["id"]),
                    reason="scan_started_by_another_operation",
                )
                return
        self.database_service.update_acquisition_scan(
            user_id, job["id"], result["state"],
            provider_job_id=result.get("job_id"), reason=result.get("reason"),
        )

    # ---- adoption ------------------------------------------------------------

    def _adoption_enabled(self, user_id: int) -> bool:
        """The reader's own opt-in (never the admin's) and the readers-may-request gate, read from the database."""
        user = self.database_service.get_user(user_id)
        if user is None or not (user.is_admin or env_truthy("ACQUISITION_ALLOW_READERS")):
            return False
        credentials = {
            ADOPT_SETTING: self.database_service.get_user_credential(user_id, ADOPT_SETTING),
            _ALLOW_GLOBAL_FALLBACK_KEY: global_fallback_allowed(self.database_service, user),
        }
        return str(resolve_setting(credentials, ADOPT_SETTING, "false")).strip().lower() in _TRUTHY_SETTING_VALUES

    def _adoption_due(self, user_id: int) -> bool:
        if not self._adoption_enabled(user_id):
            return False
        with self._adoption_claim_lock:
            if self._adoption_active.get(user_id, False):
                return False
            last = self._adoption_scanned.get(user_id)
            return last is None or utcnow() - last >= ADOPTION_SCAN_INTERVAL

    def _claim_adoption(self, user_id: int) -> bool:
        """Atomically claim this user's scan interval before performing any remote calls.

        Rejects if a scan is already active for this user regardless of elapsed time,
        or if the interval has not yet passed.
        """
        if not self._adoption_enabled(user_id):
            return False
        with self._adoption_claim_lock:
            now = utcnow()
            if self._adoption_active.get(user_id, False):
                return False
            last = self._adoption_scanned.get(user_id)
            if last is not None and now - last < ADOPTION_SCAN_INTERVAL:
                return False
            self._adoption_scanned[user_id] = now
            self._adoption_active[user_id] = True
            return True

    def _adopt_requests(self, user_id: int, client: Any) -> None:
        """Add rows for the reader's own recently fulfilled BookOrbit audiobook requests not yet followed."""
        with self._adoption_claim_lock:
            self._adoption_scanned[user_id] = utcnow()
        me = client.current_user_id()
        if me is None:
            logger.debug("📥 Acquisition: user %s BookOrbit account could not be identified; adoption skipped", user_id)
            return
        cutoff = utcnow() - GIVE_UP_AFTER
        adopted = 0
        for page in range(1, ADOPTION_MAX_PAGES + 1):
            data = client.list_requests(status="available", media_kind="audiobook", page=page, limit=ADOPTION_PAGE_SIZE)
            items = (data or {}).get("items") or []
            if not items:
                break
            for item in items:
                adopted += self._adopt_item(user_id, item, me, cutoff)
            if page * ADOPTION_PAGE_SIZE >= int((data or {}).get("total") or 0):
                break
        if adopted:
            logger.info("📥 Acquisition: user %s adopted %d BookOrbit request(s) made in BookOrbit", user_id, adopted)

    def _adopt_item(self, user_id: int, item: dict, me: int, cutoff: datetime) -> int:
        """Save one of the reader's own fulfilled audiobook requests as a row; 1 when added."""
        updated = _parse_time(item.get("updatedAt"))
        title = str(item.get("title") or "").strip()
        if (item.get("userId") != me or item.get("mediaKind") != "audiobook" or item.get("id") is None
                or not title or updated is None or updated < cutoff):
            return 0
        remote_id = str(item["id"])
        if self.database_service.find_acquisition_request(user_id, "BookOrbit", remote_id, include_dismissed=True):
            return 0
        names = [a.get("name") if isinstance(a, dict) else a for a in item.get("authors") or []]
        index = item.get("seriesIndex")
        valid_index = (
            isinstance(index, int) and not isinstance(index, bool)
            or isinstance(index, float) and math.isfinite(index) and index.is_integer()
        )
        self.database_service.add_acquisition_requests(user_id, [{
            "service": "BookOrbit", "media_kind": "audiobook", "remote_kind": "request", "remote_id": remote_id,
            "title": title, "authors": ", ".join(str(n).strip() for n in names if n and str(n).strip()) or None,
            "series_name": item.get("seriesName") or None,
            "series_index": int(index) if valid_index else None,
            "status": "in_progress", "adopted": True,
        }])
        return 1

    def _unique_ebook(self, row: dict, client: Any) -> tuple[Optional[dict], str]:
        """The reader's one eligible BookOrbit ebook for an adopted request, else (None, why not)."""
        title, authors = BookOrbitClient._normalize_string(row["title"]), _author_set(row["authors"])
        if not title or not authors:
            return None, "the request has no title or no authors"
        same = [
            info for info in client.get_all_ebooks()
            if BookOrbitClient._normalize_string(info.get("title")) == title and _author_set(info.get("authors")) == authors
        ]
        eligible = []
        wanted = _series_key(row["series_name"], row["series_index"])
        for info in same:
            held = self.database_service.get_book_by_ebook_source("BookOrbit", str(info.get("id")))
            if (
                wanted is not None
                and wanted == _series_key(info.get("seriesName"), info.get("seriesIndex"))
                and not (held and held.audio_source_id)
            ):
                eligible.append(info)
        if len(eligible) != 1:
            return None, f"{len(eligible)} eligible of {len(same)} ebooks with the same title and authors"
        found = eligible[0]
        detail = client.get_book_detail(found["id"])
        book_file = BookOrbitClient._primary_file(detail, kind="ebook") if detail else None
        if not (book_file and book_file.get("filename")):
            return None, "the matching ebook has no file"
        return {"id": str(found["id"]), "filename": book_file["filename"]}, ""

    def _adopt_arrival(self, user_id: int, row: dict, remote: dict, client: Any) -> dict:
        """Identify an adopted request's arrival, then pair it with the reader's one matching ebook or send it to review."""
        arrival, how, candidates = self._resolve_arrival(client, row, remote)
        if arrival is None:
            return self._review_ambiguous_adoption(user_id, row, candidates, client)
        changes: dict[str, Any] = {"arrived_source": "BookOrbit", "arrived_source_id": arrival["id"]}
        audio_book = self._audio_book("BookOrbit", arrival["id"])
        if audio_book and audio_book.ebook_filename:
            return {**changes, **self._refuse(user_id, row, _AUDIO_PAIRED), "dismissed": True}
        ebook, why = self._unique_ebook(row, client)
        if ebook is None:
            logger.info(
                "📥 Acquisition: user %s adopted request %s has no unique ebook match (%s); left for review",
                user_id, row["id"], why,
            )
            return {**changes, **self._review_adopted(user_id, row, arrival, client)}
        logger.info(
            "📥 Acquisition: user %s adopted request %s matched ebook %s (title, authors, series agree)",
            user_id, row["id"], ebook["id"],
        )
        partner = {"partner_source": "BookOrbit", "partner_source_id": ebook["id"], "partner_filename": ebook["filename"]}
        return {**changes, **partner, **self._pair_with_partner(user_id, {**row, **partner}, arrival, how, client)}

    def _review_ambiguous_adoption(self, user_id: int, row: dict, candidates: list, client: Any) -> dict:
        """Offer each exact audiobook edition to Discover without selecting an uncertain arrival."""
        title = BookOrbitClient._normalize_string(row["title"])
        authors = _author_set(row["authors"])
        offered = 0
        seen: set[str] = set()
        for hit in candidates:
            audio_id = str(hit.get("id"))
            if (audio_id in seen or hit.get("id") is None or not authors
                    or BookOrbitClient._normalize_string(hit.get("title")) != title
                    or _author_set(hit.get("authors")) != authors):
                continue
            seen.add(audio_id)
            if self._review_adopted(user_id, row, {"id": audio_id}, client).get("status") == "needs_look":
                offered += 1
        if offered:
            logger.info(
                "📥 Acquisition: user %s request %s sent %d candidate BookOrbit audiobooks to Discover review",
                user_id, row["id"], offered,
            )
            return {"status": "needs_look"}
        return self._refuse(user_id, row, _UNIDENTIFIED)

    def _review_adopted(self, user_id: int, row: dict, arrival: dict, client: Any) -> dict:
        """Offer the arrived audiobook to Discover's review against the title's ebooks; never pairs by itself."""
        hits = [hit for hit in client.search_ebooks(row["title"]) or [] if hit.get("id") is not None]
        view = {**row, "media_kind": "ebook", "partner_source": "BookOrbit", "partner_source_id": arrival["id"]}
        if hits and self._hand_off(user_id, view, client, hits, auto_pair=False) == "review":
            logger.info(
                "📥 Acquisition: user %s request %s sent to Discover review (%d candidate BookOrbit ebooks)",
                user_id, row["id"], len(hits),
            )
            return {"status": "needs_look"}
        return self._refuse(user_id, row, _NO_EBOOK)

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
            if status == "available" and not self._is_job_receipt(user_id, row):
                changes.update(self._on_available(user_id, row, remote, client, resolved))
            if changes["status"] != row["status"]:
                logger.info(
                    "📥 Acquisition: user %s request %s moved %s -> %s (BookOrbit status %s)",
                    user_id, row["id"], row["status"], changes["status"], status,
                )
        self._update(user_id, row, **changes)
        if status == "available" and self._is_job_receipt(user_id, row):
            self._record_bookorbit_job_arrival(user_id, row, remote, client)

    def _record_bookorbit_job_arrival(
        self, user_id: int, receipt: dict, remote: dict, client: Any,
    ) -> None:
        """Only an authoritative BookOrbit matchedBookId with fresh format and
        edition evidence may become a provider-linked library identity.
        """
        matched = remote.get("matchedBookId")
        if matched is None or isinstance(matched, bool):
            return
        job = self.database_service.get_acquisition_job(user_id, receipt["group_key"])
        if job is None:
            return
        kind = receipt["media_kind"]
        detail = client.get_book_detail(matched, force=True, fresh_only=True)
        book_file = (BookOrbitClient._primary_file(detail, kind=kind)
                     if isinstance(detail, dict) else None)
        if not isinstance(book_file, dict) or not (
            book_file.get("id") is not None or book_file.get("filename")
        ):
            return
        if kind == "ebook" and not book_file.get("filename"):
            return
        from src.utils.series_metadata import extract_series_from_library_detail

        series_name, series_index = extract_series_from_library_detail(detail)
        identity = {"source": "BookOrbit", "source_id": str(matched)}
        if kind == "ebook":
            identity["filename"] = str(book_file["filename"])
        candidate = {
            **identity,
            "title": detail.get("title") or "",
            "authors": BookOrbitClient._format_authors(detail.get("authors")),
            "series_name": series_name,
            "series_index": series_index,
            "isbn13": detail.get("isbn13") or detail.get("isbn"),
        }
        prefix = "ebook" if kind == "ebook" else "audio"
        explicit = (job.get(f"{prefix}_source") == "BookOrbit"
                    and str(job.get(f"{prefix}_source_id")) == str(matched))
        if not self._candidate_matches(job, candidate, explicit=explicit):
            return
        if not self.database_service.record_acquisition_provider_arrival(
            user_id, job["id"], receipt["id"], kind, identity,
        ):
            return
        # A separately completed job can still receive its later provider
        # provenance, but only when the provider proves it is the same edition.
        if job.get("linked_abs_id") and explicit and (
            kind != "ebook" or job.get("ebook_filename") == identity["filename"]
        ) and self.database_service.is_user_linked(user_id, job["linked_abs_id"]):
            self.database_service.set_acquisition_pairing_result(
                user_id, job["id"], abs_id=job["linked_abs_id"],
            )

    @staticmethod
    def bookorbit_decision(remote: dict) -> tuple[str, str]:
        """Return a structured reason and allowed action from verified request evidence.

        A request being approved/searching, and absence of a failure code, cannot
        prove that BookOrbit's automatic grabber is disabled. The integration key
        cannot read admin automation settings.
        """
        status = str(remote.get("status") or "").lower()
        code = str(remote.get("failureCode") or "").upper()[:100]
        if status in ("grabbed", "downloading", "importing", "available"):
            return "provider_processing", "wait"
        if status in ("rejected", "cancelled"):
            return code or "provider_closed", "none"
        if code in {"NO_DESTINATION", "NO_DESTINATION_LIBRARY", "NO_DOWNLOAD_CLIENT",
                    "SOURCE_UNCONFIGURED", "NO_SOURCES", "SOURCE_AUTH_FAILED"}:
            return code, "repair_configuration"
        if remote.get("selfServe") is True:
            return "self_serve", "pick_release"
        if code in {"AUTOMATION_DISABLED", "BELOW_SCORE_FLOOR", "PROFILE_EXCLUDED_ALL",
                    "ALL_TRIED", "ATTEMPT_LIMIT", "SEARCH_FAILED"}:
            return code, "pick_release"
        return code or "automation_unknown", "wait"

    @classmethod
    def remote_changes(cls, row: dict, remote: dict) -> dict:
        """Persist status, structured decision, and progress without the full provider payload."""
        status = remote.get("status")
        mapped = STATUS_MAP.get(status, "in_progress")
        download = remote.get("download") if isinstance(remote.get("download"), dict) else {}
        value = download.get("downloadedBytes")
        value = download.get("progressPercent") if value is None else value
        mark = f"{download.get('id')}|{download.get('grabbedAt')}|{value}" if status in ("grabbed", "downloading") and download else None
        reason, action = cls.bookorbit_decision(remote)
        return {
            "status": mapped, "remote_status": status, "status_detail": cls._detail(remote, mapped),
            "decision_code": reason, "decision_action": action,
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
        missing = [row for row in rows if self._shelfmark_remote(row, activity) is None]
        if missing and hasattr(shelfmark, "get_activity_history"):
            history = []
            for offset in (0, 50, 100, 150):
                page, error = shelfmark.get_activity_history(limit=50, offset=offset)
                if page is None:
                    logger.debug("📥 Acquisition: Shelfmark history unavailable (%s)", error)
                    break
                history.extend(page)
                if len(page) < 50:
                    break
            if history:
                activity = {**activity, "_dismissed_history": history}
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
                if str(key) == remote_id or str(payload.get("id")) == remote_id:
                    return state, payload, {}
            for entry in activity.get("_dismissed_history") or []:
                if not isinstance(entry, dict) or entry.get("item_type") != "download":
                    continue
                if remote_id in (str(entry.get("source_id")), str(entry.get("item_key", "")).partition(":")[2]):
                    state = str(entry.get("final_status") or "").lower()
                    payload = (entry.get("snapshot") or {}).get("download") or {}
                    if state in _SHELFMARK_TASK_PRECEDENCE and isinstance(payload, dict):
                        return state, payload, {}
            return None
        request = next((r for r in activity.get("requests") or [] if isinstance(r, dict) and str(r.get("id")) == remote_id), None)
        if request is None:
            for entry in activity.get("_dismissed_history") or []:
                if isinstance(entry, dict) and entry.get("item_type") == "request" and str(entry.get("request_id")) == remote_id:
                    request = (entry.get("snapshot") or {}).get("request")
                    break
        if not isinstance(request, dict):
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
                if self._is_job_receipt(user_id, row):
                    filename = PurePath(str(task.get("download_path") or "")).name
                    if filename:
                        changes["arrived_filename"] = filename
                    changes["status_detail"] = _DOWNLOADED
                    if row["media_kind"] == "audiobook":
                        self.database_service.set_acquisition_delivery_seen(user_id, row["group_key"])
                else:
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
        if row["adopted"] and not (row["partner_source_id"] or row["group_key"]):
            return self._adopt_arrival(user_id, row, remote, client)
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

    def _hand_off(self, user_id: int, row: dict, client: Any, candidates: list, auto_pair: bool = True):
        """Auto-pair a unique top match at the watcher's threshold, else save a review suggestion.

        ``auto_pair=False`` only ever saves a suggestion.

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
                intended_source = _audio_source(row["partner_source"])
                matches = [
                    m for m in scanned
                    if intended_source is not None
                    and _audio_source(m.get("audio_source")) == intended_source
                    and str(m.get("audio_source_id")) == str(row["partner_source_id"])
                ]
                top_fits = bool(scanned) and scanned[0] in matches
            else:
                candidate_ids = {str(hit["id"]) for hit in candidates if hit.get("id") is not None}
                matches = [
                    m for m in scanned
                    if _audio_source(m.get("audio_source")) == "BookOrbit"
                    and str(m.get("audio_source_id")) in candidate_ids
                ]
                top_fits = bool(scanned) and scanned[0] in matches
            if not matches:
                continue
            score = float(scanned[0].get("score") or 0.0)
            if auto_pair and top_fits and score >= shelf._threshold() and shelf._unique_top(scanned, filename, "Acquisition"):
                return self._auto_pair(user_id, row, client, scanned[0], filename, source_id, score)
            if shelf._create_pending_suggestion(
                {"title": title, "authors": authors}, filename, source_id, matches,
                origin="reading_watch", user_id=user_id, acquisition_request_id=row["id"],
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
        """Reconcile Discover approval against an exact, user-linked mapping; do not guess an edition."""
        held = self._partner_book(row)
        if held is None and row["adopted"] and row["arrived_source_id"]:
            held = self._audio_book("BookOrbit", row["arrived_source_id"])
        if held is None and row["adopted"] and not row["arrived_source_id"]:
            paired = {}
            for suggestion in self.database_service.get_acquisition_review_suggestions(user_id, row["id"]):
                metadata = suggestion.origin_metadata
                if (not suggestion.source_id.startswith("bookorbit:")
                        or _audio_source(metadata.get("source_name")) != "BookOrbit"
                        or metadata.get("grimmory_id") is None):
                    continue
                audio_id = suggestion.source_id.partition(":")[2]
                candidate = self._audio_book("BookOrbit", audio_id)
                if (candidate is not None and candidate.audio_source == "BookOrbit"
                        and candidate.audio_source_id == audio_id
                        and candidate.ebook_source == "BookOrbit"
                        and str(candidate.ebook_source_id) == str(metadata["grimmory_id"])
                        and candidate.ebook_filename
                        and self.database_service.is_user_linked(user_id, candidate.abs_id)):
                    paired[audio_id] = candidate
            if len(paired) == 1:
                audio_id, held = next(iter(paired.items()))
                logger.info("📥 Acquisition: user %s request %s paired through Discover review (mapping %s)", user_id, row["id"], held.abs_id)
                return {"status": "paired", "linked_abs_id": held.abs_id,
                        "arrived_source": "BookOrbit", "arrived_source_id": audio_id}
            return {"status": "needs_look"}
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
        if source and source_id:
            # Never resolve a different service/ID merely because its filename matches.
            return self.database_service.get_book_by_ebook_source(source, source_id)
        return self.database_service.get_book_by_ebook_filename(filename) if filename else None

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
            if (audio_book.audio_source != audio_source or str(audio_book.audio_source_id) != str(audio_id)
                    or ebook_book.ebook_source != ebook_source
                    or str(ebook_book.ebook_source_id) != str(ebook_id)
                    or ebook_book.ebook_filename != ebook_filename):
                return None, "The existing mapping has a conflicting library identity."
            if not self.database_service.is_user_linked(user_id, audio_book.abs_id):
                self.database_service.link_user_book(user_id, audio_book.abs_id)
            return audio_book.abs_id, ""
        if audio_book and audio_book.ebook_filename:
            return None, _AUDIO_PAIRED
        if ebook_book and ebook_book.audio_source_id:
            return None, "That ebook is already paired with another audiobook."
        info = client.get_audiobook_info(audio_id) if audio_source == "BookOrbit" and client is not None else None
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
