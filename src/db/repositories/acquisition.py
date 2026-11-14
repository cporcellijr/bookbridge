import json
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from ..models import AcquisitionRequest, AcquisitionJob
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class AcquisitionMixin:
    """Database operations for acquisition."""

    @staticmethod
    def _acquisition_half_fields(media_kind: str, identity: dict) -> dict:
        """Validate the explicit identity of an already available half."""
        if media_kind not in ("ebook", "audiobook") or not isinstance(identity, dict):
            raise ValueError("Invalid acquisition half")
        allowed = {"source", "source_id", "filename"} if media_kind == "ebook" else {"source", "source_id"}
        if set(identity) - allowed:
            raise ValueError("Unexpected acquisition identity fields")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        fields = {}
        for key, limit in (("source", 50), ("source_id", 255)):
            value = identity.get(key)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
                raise ValueError(f"Invalid acquisition {key}")
            fields[f"{prefix}_{key}"] = value.strip()
        if media_kind == "ebook":
            filename = identity.get("filename")
            if not isinstance(filename, str) or not filename.strip() or len(filename.strip()) > 500:
                raise ValueError("Invalid acquisition ebook filename")
            fields["ebook_filename"] = filename.strip()
        return fields

    @staticmethod
    def _acquisition_job_dict(row: AcquisitionJob, requests: list[dict]) -> dict:
        result = {c.name: getattr(row, c.name) for c in AcquisitionJob.__table__.columns}
        result["formats"] = [kind for kind in ("ebook", "audiobook") if result[f"requested_{kind}"]]
        result["review_candidates"] = {
            kind: json.loads(result[f"{prefix}_review_candidates"] or "[]")
            for kind, prefix in (("ebook", "ebook"), ("audiobook", "audio"))
        }
        result["requests"] = requests
        return result

    def create_acquisition_job(
        self, user_id: int, title: str, formats: list[str], *, authors: Optional[str] = None,
        isbn13: Optional[str] = None, series_name: Optional[str] = None, series_index: Optional[int] = None,
        ebook: Optional[dict] = None, audiobook: Optional[dict] = None, reuse: bool = False,
    ) -> dict:
        """Persist acquisition intent and explicit existing halves before calling providers."""
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 500:
            raise ValueError("Invalid acquisition title")
        if not isinstance(formats, list) or not formats or any(k not in ("ebook", "audiobook") for k in formats):
            raise ValueError("Invalid acquisition formats")
        metadata = {"authors": authors, "isbn13": isbn13, "series_name": series_name}
        for key, limit in (("authors", 500), ("isbn13", 20), ("series_name", 500)):
            value = metadata[key]
            if value is not None:
                if not isinstance(value, str) or len(value.strip()) > limit:
                    raise ValueError(f"Invalid acquisition {key}")
                metadata[key] = value.strip() or None
        if series_index is not None and (isinstance(series_index, bool) or not isinstance(series_index, int)):
            raise ValueError("Invalid acquisition series index")
        fields = {}
        if ebook is not None:
            fields.update(self._acquisition_half_fields("ebook", ebook))
        if audiobook is not None:
            fields.update(self._acquisition_half_fields("audiobook", audiobook))
        with self.get_session() as session:
            if reuse:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                candidates = session.query(AcquisitionJob).filter_by(
                    user_id=user_id, title=title.strip(), dismissed=False,
                    requested_ebook="ebook" in formats, requested_audiobook="audiobook" in formats,
                    **metadata, series_index=series_index,
                ).order_by(AcquisitionJob.created_at.desc()).all()
                for candidate in candidates:
                    if all(getattr(candidate, name) == value for name, value in fields.items()):
                        requests = session.query(AcquisitionRequest).filter_by(
                            user_id=user_id, group_key=candidate.id,
                        ).order_by(AcquisitionRequest.id).all()
                        return self._acquisition_job_dict(candidate, [self._acquisition_dict(r) for r in requests])
            row = AcquisitionJob(
                id=uuid.uuid4().hex, user_id=user_id, title=title.strip(), **metadata,
                series_index=series_index, requested_ebook="ebook" in formats,
                requested_audiobook="audiobook" in formats, **fields,
            )
            session.add(row)
            session.flush()
            return self._acquisition_job_dict(row, [])

    def get_acquisition_job(self, user_id: int, job_id: str) -> Optional[dict]:
        """Return an owned acquisition job with its provider receipts, including dismissed history."""
        with self.get_session() as session:
            row = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if row is None:
                return None
            requests = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id,
            ).order_by(AcquisitionRequest.id).all()
            return self._acquisition_job_dict(row, [self._acquisition_dict(r) for r in requests])

    def get_acquisition_jobs(self, user_id: int) -> list[dict]:
        """Return the reader's visible jobs, newest first, with their saved receipts."""
        with self.get_session() as session:
            jobs = session.query(AcquisitionJob).filter_by(user_id=user_id, dismissed=False).order_by(
                AcquisitionJob.created_at.desc(), AcquisitionJob.id.desc(),
            ).all()
            if not jobs:
                return []
            requests = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.group_key.in_([job.id for job in jobs]),
            ).order_by(AcquisitionRequest.id).all()
            grouped = defaultdict(list)
            for request in requests:
                grouped[request.group_key].append(self._acquisition_dict(request))
            return [self._acquisition_job_dict(job, grouped[job.id]) for job in jobs]

    def dismiss_acquisition_job(self, user_id: int, job_id: str) -> bool:
        """Hide an owned job without cancelling or dismissing its provider requests.

        A dismissed job never reports its scan's provider id, so scan waiters
        serialized behind its unacknowledged claim are released to claim afresh.
        """
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return False
            job.dismissed = True
            job.updated_at = utcnow()
            if job.scan_state in ("claimed", "waiting_scope") and not job.scan_job_id and job.scan_library_id:
                scope_filter = (
                    AcquisitionJob.scan_scope == job.scan_scope if job.scan_scope
                    else AcquisitionJob.user_id == user_id
                )
                session.query(AcquisitionJob).filter(
                    scope_filter,
                    AcquisitionJob.id != job_id,
                    AcquisitionJob.scan_library_id == job.scan_library_id,
                    AcquisitionJob.scan_state == "waiting_scope",
                    AcquisitionJob.scan_job_id.is_(None),
                ).update({
                    "scan_state": None, "scan_reason": None, "scan_library_id": None,
                    "scan_scope": None, "updated_at": utcnow(),
                }, synchronize_session=False)
            return True

    def set_acquisition_job_existing_half(
        self, user_id: int, job_id: str, media_kind: str, identity: dict,
    ) -> Optional[dict]:
        """Record an available half; never silently replace a previously selected identity."""
        fields = self._acquisition_half_fields(media_kind, identity)
        with self.get_session() as session:
            # Serialize identity decisions across SQLite connections, not just threads sharing this object.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return None
            if any(getattr(job, key) not in (None, value) for key, value in fields.items()):
                raise ValueError("Acquisition half already has a different identity")
            for key, value in fields.items():
                setattr(job, key, value)
            session.flush()
        return self.get_acquisition_job(user_id, job_id)

    def reserve_acquisition_submission(self, user_id: int, job_id: str, media_kind: str) -> str:
        """Persist an uncertain attempt BEFORE provider I/O and serialize competing submissions.

        The provider has no idempotency key. An interrupted call must remain uncertain
        across a restart until its acceptance can be reconciled, not be retried blindly.
        """
        if media_kind not in ("ebook", "audiobook"):
            raise ValueError("Invalid acquisition format")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return "not_found"
            if job.dismissed or not getattr(job, f"requested_{media_kind}"):
                return "not_allowed"
            receipts = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id, media_kind=media_kind,
            ).all()
            if any(row.status in ("waiting", "in_progress", "needs_look", "arrived", "paired") for row in receipts):
                return "already_submitted"
            if getattr(job, f"{prefix}_source_id") is not None:
                return "already_owned"
            if getattr(job, f"{prefix}_submission_state") == "uncertain":
                return "submission_uncertain"
            setattr(job, f"{prefix}_submission_state", "uncertain")
            setattr(job, f"{prefix}_submission_reason", "submission_may_have_been_accepted")
            job.updated_at = utcnow()
            return "reserved"

    def set_acquisition_submission_state(
        self, user_id: int, job_id: str, media_kind: str, state: str, reason: Optional[str] = None,
        *, confirmed_rejection: bool = False,
    ) -> Optional[dict]:
        """Store a choice/refusal without clearing an in-flight or accepted submission.

        Only a definite response to a reserved provider POST may end uncertainty as
        failed. Preflight checks must never unlock an ambiguous provider attempt.
        """
        if media_kind not in ("ebook", "audiobook") or state not in (
            "needs_release", "failed", "uncertain", "accepted", "needs_identification",
        ):
            raise ValueError("Invalid acquisition state")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 100):
            raise ValueError("Invalid acquisition reason")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id, dismissed=False).first()
            if job is None:
                return None
            previous = getattr(job, f"{prefix}_submission_state")
            if previous != "accepted" and (
                previous != "uncertain" or state in ("uncertain", "accepted")
                or (confirmed_rejection and state == "failed")
            ):
                setattr(job, f"{prefix}_submission_state", state)
                setattr(job, f"{prefix}_submission_reason", reason)
                job.updated_at = utcnow()
        return self.get_acquisition_job(user_id, job_id)

    def add_acquisition_request_to_job(self, user_id: int, job_id: str, fields: dict) -> Optional[dict]:
        """Atomically attach a provider receipt without stealing another job or reviving history."""
        if not isinstance(fields, dict):
            raise ValueError("Invalid acquisition receipt")
        allowed = {"service", "media_kind", "remote_kind", "remote_id", "status", "remote_status", "status_detail", "decision_code", "decision_action"}
        if set(fields) - allowed:
            raise ValueError("Unexpected acquisition receipt fields")
        fields = dict(fields)
        if fields.get("service") not in ("BookOrbit", "Shelfmark"):
            raise ValueError("Invalid acquisition service")
        kind = fields.get("media_kind")
        if kind not in ("ebook", "audiobook"):
            raise ValueError("Invalid acquisition receipt format")
        remote_id = fields.get("remote_id")
        if isinstance(remote_id, bool) or not isinstance(remote_id, (str, int)) or not str(remote_id).strip():
            raise ValueError("Invalid acquisition remote ID")
        fields["remote_id"] = str(remote_id).strip()
        if len(fields["remote_id"]) > 255:
            raise ValueError("Invalid acquisition remote ID")
        if fields.get("remote_kind") not in ("request", "download"):
            raise ValueError("Invalid acquisition remote kind")
        statuses = {"waiting", "in_progress", "needs_look", "arrived", "paired", "declined", "failed", "cancelled"}
        if not isinstance(fields.get("status", "waiting"), str) or fields.get("status", "waiting") not in statuses:
            raise ValueError("Invalid acquisition status")
        for key, limit in (("remote_status", 50), ("status_detail", None)):
            value = fields.get(key)
            if value is not None and (not isinstance(value, str) or (limit and len(value) > limit)):
                raise ValueError(f"Invalid acquisition {key}")
        code = fields.get("decision_code")
        action = fields.get("decision_action")
        if code is not None and (not isinstance(code, str) or len(code) > 100):
            raise ValueError("Invalid acquisition decision code")
        if action is not None and action not in ("wait", "pick_release", "repair_configuration", "none"):
            raise ValueError("Invalid acquisition decision action")
        with self.get_session() as session:
            # Claim the remote handle before another connection can attach or insert the same receipt.
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None:
                return None
            if not getattr(job, f"requested_{kind}"):
                raise ValueError("Acquisition receipt is not a requested format")
            if job.dismissed:
                raise ValueError("Acquisition job is dismissed")
            active = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id, media_kind=kind,
            ).all()
            if any(r.remote_id != fields["remote_id"] and r.status in (
                "waiting", "in_progress", "needs_look", "arrived", "paired",
            ) for r in active):
                raise ValueError("Acquisition format already has an active receipt")
            prefix = "audio" if kind == "ebook" else "ebook"
            partner = {
                "partner_source": getattr(job, f"{prefix}_source"),
                "partner_source_id": getattr(job, f"{prefix}_source_id"),
                "partner_filename": job.ebook_filename if prefix == "ebook" else None,
            }
            rows = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, service=fields["service"], remote_id=fields["remote_id"],
            ).all()
            if len(rows) > 1:
                raise ValueError("Acquisition remote receipt is ambiguous")
            if rows:
                row = rows[0]
                if (row.dismissed or (row.adopted and row.group_key is None)
                        or row.group_key not in (None, job_id) or row.media_kind != kind):
                    raise ValueError("Acquisition receipt already belongs to another job or history")
                if row.remote_kind != fields["remote_kind"]:
                    raise ValueError("Acquisition remote kind conflicts with the saved receipt")
                if any(getattr(row, key) not in (None, value) for key, value in partner.items()):
                    raise ValueError("Acquisition receipt has a different pairing partner")
                row.group_key = job_id
                for key, value in partner.items():
                    setattr(row, key, value)
                fresh_status = fields.get("status", "waiting")
                preserve = (
                    row.status == "paired"
                    or (row.status == "arrived" and fresh_status not in ("arrived", "paired"))
                    or (row.status in ("in_progress", "needs_look") and fresh_status == "waiting")
                )
                if not preserve:
                    for key in ("status", "remote_status", "status_detail", "decision_code", "decision_action"):
                        if key in fields:
                            setattr(row, key, fields[key])
            else:
                row = AcquisitionRequest(
                    user_id=user_id, group_key=job_id, title=job.title, authors=job.authors,
                    isbn13=job.isbn13, series_name=job.series_name, series_index=job.series_index,
                    **partner, **fields,
                )
                session.add(row)
            job.updated_at = utcnow()
            prefix = "ebook" if kind == "ebook" else "audio"
            setattr(job, f"{prefix}_submission_state", "accepted")
            setattr(job, f"{prefix}_submission_reason", None)
            session.flush()
            return self._acquisition_dict(row)

    def get_unpaired_acquisition_jobs(self, user_id: int, created_after: Optional[datetime] = None) -> list[dict]:
        """Reader jobs awaiting pairing or outstanding tracked scan outcomes.

        Jobs created before ``created_after`` are given up on, except for an
        outstanding scan, whose outcome is always tracked.
        """
        from sqlalchemy import and_, exists, or_, true
        with self.get_session() as session:
            unlinked_arrival = exists().where(and_(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.group_key == AcquisitionJob.id,
                AcquisitionRequest.status == "arrived",
                AcquisitionRequest.remote_status.in_(("complete", "available")),
                AcquisitionRequest.arrived_source_id.is_(None),
                AcquisitionRequest.linked_abs_id.is_(None),
                AcquisitionRequest.dismissed.is_(False),
            ))
            recent = AcquisitionJob.created_at >= created_after if created_after is not None else true()
            jobs = session.query(AcquisitionJob).filter(
                AcquisitionJob.user_id == user_id,
                AcquisitionJob.dismissed.is_(False),
                or_(AcquisitionJob.scan_state.in_(("claimed", "accepted", "coalesced", "waiting_scope", "uncertain")),
                    and_(recent, or_(AcquisitionJob.linked_abs_id.is_(None), unlinked_arrival))),
            ).order_by(AcquisitionJob.created_at).all()
            if not jobs:
                return []
            requests = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.group_key.in_([job.id for job in jobs]),
            ).order_by(AcquisitionRequest.id).all()
            grouped = defaultdict(list)
            for row in requests:
                grouped[row.group_key].append(self._acquisition_dict(row))
            return [self._acquisition_job_dict(job, grouped[job.id]) for job in jobs]

    def set_acquisition_delivery_seen(self, user_id: int, job_id: str) -> bool:
        """Remember the first verified Shelfmark audio delivery time, across restarts."""
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return False
            if job.audio_delivered_at is None:
                job.audio_delivered_at = utcnow()
            return True

    def set_acquisition_library_state(
        self, user_id: int, job_id: str, media_kind: str, state: str,
        reason: Optional[str] = None, *, candidates: Optional[list[dict]] = None,
    ) -> bool:
        """Persist why a requested half is waiting, indexed, or needs edition review."""
        if media_kind not in ("ebook", "audiobook") or state not in (
            "waiting_import", "indexed", "needs_review", "catalog_unavailable",
        ):
            raise ValueError("Invalid library state")
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return False
            old = getattr(job, f"{prefix}_library_state")
            # A transient outage cannot revoke a prior verified identity. A
            # successful fresh lookup proving the file missing may do so.
            if old == "indexed" and state == "catalog_unavailable":
                return False
            setattr(job, f"{prefix}_library_state", state)
            setattr(job, f"{prefix}_library_reason", (reason or None)[:255] if reason else None)
            if candidates is not None:
                safe = [{k: value for k, value in hit.items() if k in (
                    "source", "source_id", "filename", "title", "authors", "series_name", "series_index",
                )} for hit in candidates[:20]]
                setattr(job, f"{prefix}_review_candidates", json.dumps(safe, default=str))
            job.updated_at = utcnow()
            return True

    def confirm_acquisition_arrival(
        self, user_id: int, job_id: str, media_kind: str, identity: dict,
        *, receipt_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Atomically retain one canonical library identity and optional provider provenance.

        Only an explicitly owned receipt may be decorated. An already-owned copy
        creates no receipt; a conflicting edition is never replaced by a later poll.
        """
        fields = self._acquisition_half_fields(media_kind, identity)
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return None
            if any(getattr(job, key) not in (None, value) for key, value in fields.items()):
                raise ValueError("Acquisition identity conflicts with a previously confirmed edition")
            receipt = None
            if receipt_id is not None:
                receipt = session.query(AcquisitionRequest).filter_by(
                    id=receipt_id, user_id=user_id, group_key=job_id, media_kind=media_kind,
                ).first()
                if receipt is None:
                    raise ValueError("Receipt not owned by acquisition job")
                if receipt.remote_status not in ("complete", "available") and receipt.status not in ("arrived", "paired"):
                    raise ValueError("Provider receipt does not confirm delivery")
                if (receipt.arrived_source and receipt.arrived_source != identity["source"]
                        or receipt.arrived_source_id and receipt.arrived_source_id != identity["source_id"]):
                    raise ValueError("Receipt has a different library identity")
            for key, value in fields.items():
                setattr(job, key, value)
            setattr(job, f"{prefix}_library_state", "indexed")
            setattr(job, f"{prefix}_library_reason", None)
            setattr(job, f"{prefix}_review_candidates", None)
            if receipt is not None:
                receipt.arrived_source = identity["source"]
                receipt.arrived_source_id = identity["source_id"]
                if media_kind == "ebook":
                    receipt.arrived_filename = identity["filename"]
                if receipt.status == "arrived":
                    receipt.status_detail = None
            job.updated_at = utcnow()
            session.flush()
            receipts = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id,
            ).order_by(AcquisitionRequest.id).all()
            return self._acquisition_job_dict(job, [self._acquisition_dict(row) for row in receipts])

    def record_acquisition_provider_arrival(
        self, user_id: int, job_id: str, request_id: int, media_kind: str,
        identity: dict,
    ) -> bool:
        """Store a verified provider-to-library handle, even if it conflicts with
        the reader's already-owned half; never replace the job's chosen edition.
        """
        fields = self._acquisition_half_fields(media_kind, identity)
        prefix = "ebook" if media_kind == "ebook" else "audio"
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(
                id=job_id, user_id=user_id, dismissed=False,
            ).first()
            receipt = session.query(AcquisitionRequest).filter_by(
                id=request_id, user_id=user_id, group_key=job_id, media_kind=media_kind,
            ).first()
            if (job is None or receipt is None or receipt.service != "BookOrbit"
                    or receipt.remote_status != "available"):
                return False
            if receipt.arrived_source and receipt.arrived_source != identity["source"]:
                return False
            if receipt.arrived_source_id and receipt.arrived_source_id != identity["source_id"]:
                return False
            conflicts = any(
                getattr(job, key) not in (None, value) for key, value in fields.items()
            )
            receipt.arrived_source = identity["source"]
            receipt.arrived_source_id = identity["source_id"]
            if media_kind == "ebook":
                receipt.arrived_filename = identity["filename"]
            if conflicts:
                receipt.status = "needs_look"
                receipt.status_detail = "Provider delivered a different library edition; review required"
            elif receipt.status not in ("paired",):
                receipt.status = "arrived"
                receipt.status_detail = None
            job.updated_at = utcnow()
            return True

    def set_acquisition_pairing_result(
        self, user_id: int, job_id: str, *, abs_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Commit the mapping result without changing unrelated in-flight receipts."""
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed:
                return False
            if abs_id and (not job.audio_source_id or not job.ebook_source_id or not job.ebook_filename):
                return False
            if abs_id and job.linked_abs_id and job.linked_abs_id != abs_id:
                return False
            job.pairing_state = "paired" if abs_id else "needs_review"
            job.pairing_reason = None if abs_id else (reason or "Pairing needs review")[:255]
            if abs_id:
                job.linked_abs_id = abs_id
                for receipt in session.query(AcquisitionRequest).filter_by(
                    user_id=user_id, group_key=job_id,
                ).all():
                    if receipt.status in ("arrived", "paired") and receipt.arrived_source_id:
                        receipt.status = "paired"
                        receipt.linked_abs_id = abs_id
                        receipt.status_detail = None
                    elif (receipt.status == "arrived" and receipt.remote_status in ("complete", "available")
                          and not receipt.linked_abs_id):
                        # Completed before this pairing, so not a "late" arrival. It stays
                        # unattributed: its delivered file is not proven to be a library copy.
                        receipt.linked_abs_id = abs_id
            job.updated_at = utcnow()
            return True

    def record_acquisition_scan_wait(self, user_id: int, job_id: str, state: str, reason: str) -> bool:
        """Record a configuration/permission wait without altering an outstanding scan."""
        if state not in ("scheduled", "refused", "unavailable", "invalid"):
            raise ValueError("Invalid pre-scan state")
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.scan_state in (
                "claimed", "accepted", "coalesced", "uncertain", "waiting_scope", "completed",
            ):
                return False
            job.scan_state = state
            job.scan_reason = reason[:255]
            job.updated_at = utcnow()
            return True

    def claim_acquisition_scan(self, user_id: int, job_id: str, library_id: str,
                               *, scan_scope: Optional[str] = None,
                               coalesce_job_id: Optional[str] = None) -> dict:
        """Serializable, durable single-scan claim, including same-reader coalescing.

        Claims precede POST; transactions end before any provider I/O. A missing
        acknowledgement is intentionally nonretryable without independent evidence.
        """
        if not isinstance(library_id, str) or not library_id.isdigit() or int(library_id) <= 0:
            return {"state": "invalid_library"}
        if scan_scope is not None and (not isinstance(scan_scope, str) or len(scan_scope) > 500):
            return {"state": "invalid_scope"}
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.audio_delivered_at is None:
                return {"state": "not_ready"}
            if job.scan_state in ("claimed", "accepted", "coalesced", "waiting_scope", "uncertain"):
                # Another caller holds this job's claim; never report it as this caller's win.
                return {"state": "already_claimed", "scan_state": job.scan_state,
                        "job_id": job.scan_job_id, "library_id": job.scan_library_id}
            if job.scan_state == "completed" and (job.scan_attempts or 0) >= 2:
                return {"state": "exhausted"}
            if job.scan_state == "completed" and job.scan_completed_at and utcnow() - job.scan_completed_at < timedelta(minutes=5):
                return {"state": "cooldown"}
            # Coalesce a scan on the same BookOrbit server/library even if
            # two authorized readers independently requested that destination.
            # Without a known server scope, coalesce ONLY within this reader.
            scope_filter = (
                AcquisitionJob.scan_scope == scan_scope if scan_scope
                else AcquisitionJob.user_id == user_id
            )
            others = session.query(AcquisitionJob).filter(
                scope_filter,
                AcquisitionJob.id != job_id,
                AcquisitionJob.scan_library_id == library_id,
                AcquisitionJob.scan_state.in_(("claimed", "accepted", "coalesced", "waiting_scope")),
                AcquisitionJob.dismissed.is_(False),
            ).order_by(AcquisitionJob.scan_claimed_at.desc()).all()
            if others:
                first = others[0]
                job.scan_library_id = library_id
                job.scan_scope = scan_scope
                job.scan_job_id = first.scan_job_id
                job.scan_state = "coalesced" if first.scan_job_id else "waiting_scope"
                job.scan_reason = "sharing_library_scan" if first.scan_job_id else "other_scan_claim_pending"
                job.scan_claimed_at = utcnow()
                job.scan_attempts = (job.scan_attempts or 0) + 1
                return {"state": job.scan_state, "job_id": job.scan_job_id, "library_id": library_id}
            job.scan_library_id = library_id
            job.scan_scope = scan_scope
            job.scan_job_id = str(coalesce_job_id) if coalesce_job_id is not None else None
            job.scan_state = "coalesced" if coalesce_job_id is not None else "claimed"
            job.scan_reason = "sharing_existing_scan" if coalesce_job_id is not None else "awaiting_scan_acknowledgement"
            # Participating in an older/other job counts toward the two-cycle
            # bound even when this reader did not create a new POST.
            job.scan_attempts = (job.scan_attempts or 0) + 1
            job.scan_claimed_at = utcnow()
            job.scan_completed_at = None
            job.updated_at = utcnow()
            return {"state": job.scan_state, "library_id": library_id, "job_id": job.scan_job_id}

    def claim_acquisition_scan_retry(
        self, user_id: int, job_id: str, library_id: str, scan_scope: str, *,
        expected_state: str, expected_job_id: Optional[str],
        expected_library_id: Optional[str], expected_scan_scope: Optional[str],
    ) -> dict:
        """Atomically claim an explicitly requested retry from a settled scan state."""
        from sqlalchemy import or_

        if (not isinstance(library_id, str) or not library_id.isdigit() or int(library_id) <= 0
                or not isinstance(scan_scope, str) or not scan_scope or len(scan_scope) > 500):
            return {"state": "invalid_scope_or_library"}
        allowed = ("failed", "completed", "refused", "invalid", "unavailable", "conflict")
        if expected_state not in allowed:
            return {"state": "not_retryable"}
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.audio_delivered_at is None:
                return {"state": "not_ready"}
            if job.audio_library_state == "indexed" or job.linked_abs_id:
                return {"state": "audiobook_already_indexed"}
            receipt = session.query(AcquisitionRequest).filter_by(
                user_id=user_id, group_key=job_id, service="Shelfmark", media_kind="audiobook",
                remote_status="complete",
            ).first()
            if receipt is None:
                return {"state": "not_ready"}
            if (job.scan_state != expected_state
                    or job.scan_job_id != expected_job_id
                    or job.scan_library_id != expected_library_id
                    or job.scan_scope != expected_scan_scope):
                return {"state": "changed"}
            if (job.scan_attempts or 0) >= 2:
                return {"state": "exhausted"}
            if job.scan_library_id and job.scan_library_id != library_id:
                return {"state": "destination_changed"}
            if job.scan_scope and job.scan_scope != scan_scope:
                return {"state": "scope_changed"}
            if job.scan_job_id and not job.scan_scope:
                return {"state": "scope_unverified"}
            if job.scan_state == "conflict" and job.scan_job_id:
                return {"state": "operation_unverified"}
            if not job.scan_job_id and job.scan_state in ("failed", "completed"):
                return {"state": "operation_unverified"}

            active = session.query(AcquisitionJob).filter(
                AcquisitionJob.id != job_id,
                AcquisitionJob.scan_library_id == library_id,
                AcquisitionJob.scan_state.in_(("claimed", "accepted", "coalesced", "waiting_scope")),
                or_(AcquisitionJob.scan_scope == scan_scope, AcquisitionJob.scan_scope.is_(None)),
                AcquisitionJob.dismissed.is_(False),
            ).first()
            if active is not None:
                return {"state": "scan_already_running"}

            # Only a settled old operation (or a refusal before acceptance) can
            # reach here. Its exact ID was checked against live history first.
            job.scan_library_id = library_id
            job.scan_scope = scan_scope
            job.scan_job_id = None
            job.scan_state = "claimed"
            job.scan_reason = "user_requested_scan_retry"
            job.scan_claimed_at = utcnow()
            job.scan_completed_at = None
            job.scan_attempts = (job.scan_attempts or 0) + 1
            job.updated_at = utcnow()
            return {"state": "claimed", "library_id": library_id}

    def update_acquisition_scan(
        self, user_id: int, job_id: str, state: str, *,
        provider_job_id: Optional[str] = None, reason: Optional[str] = None,
    ) -> bool:
        """Record a scan transition; preserve destination and reject unrelated owners."""
        if state not in ("claimed", "accepted", "coalesced", "waiting_scope", "completed", "failed", "refused", "uncertain", "conflict"):
            raise ValueError("Invalid scan state")
        with self.get_session() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            job = session.query(AcquisitionJob).filter_by(id=job_id, user_id=user_id).first()
            if job is None or job.dismissed or job.scan_state is None:
                return False
            if state == "claimed" and job.scan_state != "claimed":
                job.scan_reason = (reason or None)[:255] if reason else None
                job.updated_at = utcnow()
                return True
            if provider_job_id is not None:
                if job.scan_job_id and job.scan_job_id != str(provider_job_id):
                    return False
                job.scan_job_id = str(provider_job_id)
            if job.scan_state in ("completed", "failed", "refused", "uncertain") and state in ("accepted", "coalesced"):
                return False
            prior_state = job.scan_state
            job.scan_state = state
            job.scan_reason = (reason or None)[:255] if reason else None
            if provider_job_id is not None and state in ("accepted", "coalesced"):
                # Share only a provider-acknowledged operation with the exact
                # server/library waiters that were serialized behind this claim.
                scope_filter = (
                    AcquisitionJob.scan_scope == job.scan_scope if job.scan_scope
                    else AcquisitionJob.user_id == user_id
                )
                session.query(AcquisitionJob).filter(
                    scope_filter,
                    AcquisitionJob.id != job_id,
                    AcquisitionJob.scan_library_id == job.scan_library_id,
                    AcquisitionJob.scan_state == "waiting_scope",
                    AcquisitionJob.scan_job_id.is_(None),
                    AcquisitionJob.dismissed.is_(False),
                ).update({
                    "scan_job_id": str(provider_job_id),
                    "scan_state": "coalesced",
                    "scan_reason": "sharing_library_scan",
                    "updated_at": utcnow(),
                }, synchronize_session=False)
            if state in ("completed", "failed") and prior_state != state:
                job.scan_completed_at = utcnow()
            job.updated_at = utcnow()
            return True

    @staticmethod
    def _acquisition_dict(row: AcquisitionRequest) -> dict:
        return {c.name: getattr(row, c.name) for c in AcquisitionRequest.__table__.columns}

    def add_acquisition_requests(self, user_id: int, rows: list[dict]) -> list[dict]:
        """Save one request row per dict for ``user_id`` and return the saved rows."""
        with self.get_session() as session:
            saved = [AcquisitionRequest(user_id=user_id, **fields) for fields in rows]
            session.add_all(saved)
            session.flush()
            return [self._acquisition_dict(row) for row in saved]

    def get_acquisition_requests(self, user_id: int) -> list[dict]:
        """Return the user's non-dismissed requests, newest first."""
        with self.get_session() as session:
            rows = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.dismissed.is_(False),
            ).order_by(AcquisitionRequest.id.desc()).all()
            return [self._acquisition_dict(row) for row in rows]

    def get_acquisition_request(self, user_id: int, request_id: int) -> Optional[dict]:
        """Return one of the user's requests, or None when it is not theirs."""
        with self.get_session() as session:
            row = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.id == request_id, AcquisitionRequest.user_id == user_id,
            ).first()
            return self._acquisition_dict(row) if row else None

    def find_acquisition_request(self, user_id: int, service: str, remote_id: str,
                                 include_dismissed: bool = False) -> Optional[dict]:
        """Return the user's request for a service's remote id, if any (dismissed ones only on request)."""
        with self.get_session() as session:
            query = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.service == service,
                AcquisitionRequest.remote_id == remote_id,
            )
            if not include_dismissed:
                query = query.filter(AcquisitionRequest.dismissed.is_(False))
            row = query.first()
            return self._acquisition_dict(row) if row else None

    def get_refreshable_acquisition_requests(self, user_id: int, arrived_since) -> list[dict]:
        """Return the user's BookOrbit and Shelfmark requests still worth checking.

        That is every open request, plus a request that arrived on or after
        ``arrived_since`` and still has to be paired (it has a partner or a
        "Both" sibling but no mapping yet), or a Shelfmark download whose book
        the library has not shown yet.
        """
        from sqlalchemy import and_, or_

        with self.get_session() as session:
            rows = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.user_id == user_id,
                AcquisitionRequest.service.in_(("BookOrbit", "Shelfmark")),
                AcquisitionRequest.remote_id.isnot(None),
                AcquisitionRequest.dismissed.is_(False),
                or_(
                    AcquisitionRequest.status.in_(("waiting", "in_progress", "needs_look")),
                    and_(
                        AcquisitionRequest.status == "arrived",
                        AcquisitionRequest.linked_abs_id.is_(None),
                        AcquisitionRequest.created_at >= arrived_since,
                        or_(
                            AcquisitionRequest.partner_source_id.isnot(None),
                            AcquisitionRequest.group_key.isnot(None),
                            and_(
                                AcquisitionRequest.service == "BookOrbit",
                                AcquisitionRequest.adopted.is_(True),
                            ),
                            and_(
                                AcquisitionRequest.service == "Shelfmark",
                                AcquisitionRequest.arrived_source_id.is_(None),
                            ),
                        ),
                    ),
                ),
            ).order_by(AcquisitionRequest.id.desc()).all()
            return [self._acquisition_dict(row) for row in rows]

    def update_acquisition_request(self, user_id: int, request_id: int, **fields) -> Optional[dict]:
        """Update fields on one of the user's requests; None when it is not theirs."""
        with self.get_session() as session:
            row = session.query(AcquisitionRequest).filter(
                AcquisitionRequest.id == request_id, AcquisitionRequest.user_id == user_id,
            ).first()
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            session.flush()
            return self._acquisition_dict(row)
