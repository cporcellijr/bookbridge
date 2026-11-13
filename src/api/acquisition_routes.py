"""User-scoped API for asking BookOrbit or Shelfmark to fetch a book a search did not find."""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any, Optional

from flask import current_app, jsonify, request

from src.api.shelfmark_client import release_media_kind
from src.services.acquisition_service import OPEN_STATUSES, AcquisitionService, is_stalled
from src.utils.config_loader import env_truthy

logger = logging.getLogger(__name__)

_MEDIA_KINDS = ("ebook", "audiobook")
_PUBLIC_FIELDS = (
    "id", "service", "media_kind", "remote_kind", "title", "authors", "status", "status_detail", "remote_status",
    "partner_source", "partner_source_id", "group_key", "arrived_source_id", "linked_abs_id",
    "decision_code", "decision_action",
)

_WEB_MODULE = None


def _web_server():
    """Return the web module that registered these routes (``__main__`` in production)."""
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    from importlib import import_module

    return import_module("src.web_server")


def init_acquisition_routes(app: Any, web_module: Any = None) -> None:
    """Register the acquisition API routes on ``app``."""
    global _WEB_MODULE
    if web_module is not None:
        _WEB_MODULE = web_module
    rules = (
        ("/api/acquisition/options", "acquisition_options", acquisition_options, ["GET"]),
        ("/api/acquisition/bookorbit", "acquisition_bookorbit", acquisition_bookorbit, ["POST"]),
        ("/api/acquisition/shelfmark/releases", "acquisition_shelfmark_releases", acquisition_shelfmark_releases, ["GET"]),
        ("/api/acquisition/shelfmark", "acquisition_shelfmark", acquisition_shelfmark, ["POST"]),
        ("/api/acquisition/mine", "acquisition_mine", acquisition_mine, ["GET"]),
        ("/api/acquisition/jobs", "acquisition_jobs", acquisition_jobs, ["GET"]),
        ("/api/acquisition/jobs/<string:job_id>", "acquisition_job", acquisition_job, ["GET"]),
        ("/api/acquisition/jobs/<string:job_id>/dismiss", "acquisition_job_dismiss", acquisition_job_dismiss, ["POST"]),
        ("/api/acquisition/jobs/<string:job_id>/<string:media_kind>/review", "acquisition_job_review", acquisition_job_review, ["POST"]),
        ("/api/acquisition/jobs/<string:job_id>/scan/retry", "acquisition_job_scan_retry", acquisition_job_scan_retry, ["POST"]),
        ("/api/acquisition/<int:request_id>/releases", "acquisition_releases", acquisition_releases, ["GET"]),
        ("/api/acquisition/<int:request_id>/grab", "acquisition_grab", acquisition_grab, ["POST"]),
        ("/api/acquisition/<int:request_id>/cancel", "acquisition_cancel", acquisition_cancel, ["POST"]),
        ("/api/acquisition/<int:request_id>/dismiss", "acquisition_dismiss", acquisition_dismiss, ["POST"]),
    )
    for rule, endpoint, view, methods in rules:
        app.add_url_rule(rule, endpoint, view, methods=methods)


def _api_error(message: str, status: int):
    return jsonify({"error": message}), status


def _is_allowed(web) -> bool:
    """Admins always; readers only while ACQUISITION_ALLOW_READERS is on."""
    if current_app.config.get("LOGIN_DISABLED"):
        return True
    user = web.current_user()
    return bool(user and (user.is_admin or env_truthy("ACQUISITION_ALLOW_READERS")))


def _bookorbit_client(web) -> Optional[Any]:
    """The configured BookOrbit client of the requesting user's own bundle, or None."""
    try:
        client = getattr(web._scan_user_clients(), "bookorbit_client", None)
    except PermissionError:
        return None
    return client if client is not None and client.is_configured() else None


def _shelfmark_client(web) -> Optional[Any]:
    """The requesting user's own Shelfmark client (not yet checked for a login), or None."""
    try:
        return getattr(web._scan_user_clients(), "shelfmark_client", None)
    except PermissionError:
        return None


def _public(row: dict) -> dict:
    return {**{key: row[key] for key in _PUBLIC_FIELDS}, "stalled": is_stalled(row)}


def _text(value: Any, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _request_item(result: Any) -> dict:
    """The request object inside a create answer (``{request}`` or the object itself)."""
    if isinstance(result, dict):
        inner = result.get("request")
        return inner if isinstance(inner, dict) else result
    return {}


def acquisition_options():
    """Which services and formats this reader can use, with a reason code when none."""
    web = _web_server()
    if web.get_current_user_id() is None:
        return _api_error("authentication_required", 401)
    service = {"usable": False, "reason": None, "formats": []}
    if not _is_allowed(web):
        service["reason"] = "toggle_off"
    else:
        client = _bookorbit_client(web)
        if client is None:
            service["reason"] = "bookorbit_not_configured"
        else:
            sources = client.request_sources()
            if sources is None:
                service["reason"] = "no_request_permission"
            elif not (isinstance(sources, dict) and (sources.get("enabled") or 0) > 0):
                service["reason"] = "no_sources"
            else:
                service.update(usable=True, formats=list(_MEDIA_KINDS), can_pick=bool(client.can_pick_releases()))
    return jsonify({"services": {"bookorbit": service, "shelfmark": _shelfmark_service(web)}})


def _shelfmark_service(web) -> dict:
    """Whether Shelfmark is usable for this reader and in which mode per format (one policy call)."""
    service = {"usable": False, "reason": None, "formats": {}, "find_formats": []}
    client = _shelfmark_client(web)
    if not _is_allowed(web):
        service["reason"] = "toggle_off"
    elif client is None or not client.is_enabled():
        service["reason"] = "shelfmark_not_configured"
    else:
        modes, error = (client.available_modes() if hasattr(client, "available_modes")
                        else client.get_policy())
        formats = {kind: mode for kind, mode in (modes or {}).items() if mode != "blocked"}
        if modes is None:
            service["reason"] = error
        elif not formats:
            service["reason"] = "policy_blocked"
        else:
            find_formats = [kind for kind in _MEDIA_KINDS
                            if kind in formats and hasattr(client, "can_find_releases")
                            and client.can_find_releases(kind)]
            service.update(usable=True, formats=formats, find_formats=find_formats)
    return service


def _valid_submission_formats(kinds: Any) -> bool:
    """Reject malformed format elements before attempting hash-based deduplication."""
    return (isinstance(kinds, list) and bool(kinds)
            and all(isinstance(kind, str) and kind in _MEDIA_KINDS for kind in kinds)
            and len(set(kinds)) == len(kinds))


def _parse_create_body(payload: dict) -> tuple[Optional[dict], Optional[str]]:
    """Validate a create body; (fields, None) or (None, error code)."""
    title = _text(payload.get("title"))
    if not title:
        return None, "title_required"
    formats = payload.get("formats")
    if not isinstance(formats, list) or not formats or any(f not in _MEDIA_KINDS for f in formats):
        return None, "invalid_formats"
    authors = payload.get("authors")
    if isinstance(authors, list):
        authors = [a.strip() for a in authors if isinstance(a, str) and a.strip()]
    else:
        authors = [_text(authors)] if _text(authors) else []
    series_index = payload.get("series_index")
    if series_index in (None, ""):
        series_index = None
    else:
        if isinstance(series_index, bool):
            return None, "invalid_series_index"
        if isinstance(series_index, float) and (
            not math.isfinite(series_index) or not series_index.is_integer()
        ):
            return None, "invalid_series_index"
        try:
            series_index = int(series_index)
        except (TypeError, ValueError, OverflowError):
            return None, "invalid_series_index"
    partner = payload.get("partner")
    partner = partner if isinstance(partner, dict) else {}
    return {
        "title": title,
        "formats": [f for f in _MEDIA_KINDS if f in formats],
        "authors": authors,
        "isbn13": _text(payload.get("isbn13"), 20) or None,
        "series_name": _text(payload.get("series_name")) or None,
        "series_index": series_index,
        "partner_source": _text(partner.get("source"), 50) or None,
        "partner_source_id": _text(partner.get("source_id"), 255) or None,
        "partner_filename": _text(partner.get("filename")) or None,
    }, None


def _intent(web, user_id: int, payload: dict, submit_formats: list[str]) -> tuple[Optional[dict], Optional[dict], Optional[tuple]]:
    """Resolve or save a reader's durable intent, including explicit already-owned halves."""
    job_id = payload.get("job_id")
    if job_id is not None:
        if not isinstance(job_id, str) or len(job_id) != 32:
            return None, None, _api_error("invalid_job", 400)
        job = web.database_service.get_acquisition_job(user_id, job_id)
        if job is None:
            return None, None, _api_error("not_found", 404)
        if job["dismissed"]:
            return None, None, _api_error("job_dismissed", 409)
        if any(kind not in job["formats"] for kind in submit_formats):
            return None, None, _api_error("format_not_requested", 409)
        if payload.get("title") and _text(payload["title"]) != job["title"]:
            return None, None, _api_error("job_metadata_changed", 409)
        for field in ("isbn13", "series_name", "series_index"):
            if field in payload and payload[field] != job[field]:
                return None, None, _api_error("job_metadata_changed", 409)
        if "authors" in payload:
            authored = payload["authors"]
            authored = authored if isinstance(authored, list) else [authored]
            if ", ".join(a.strip() for a in authored if isinstance(a, str) and a.strip()) != (job["authors"] or ""):
                return None, None, _api_error("job_metadata_changed", 409)
        if payload.get("partner") or payload.get("existing"):
            return None, None, _api_error("job_identity_changed", 409)
        fields = {
            "title": job["title"], "formats": job["formats"],
            "authors": [a.strip() for a in (job["authors"] or "").split(",") if a.strip()],
            "isbn13": job["isbn13"], "series_name": job["series_name"],
            "series_index": job["series_index"],
        }
        return job, fields, None

    fields, error = _parse_create_body({**payload, "formats": payload.get("formats", submit_formats)})
    if error:
        return None, None, _api_error(error, 400)
    if any(kind not in fields["formats"] for kind in submit_formats):
        return None, None, _api_error("format_not_requested", 400)
    existing = payload.get("existing")
    if existing is not None and not isinstance(existing, dict):
        return None, None, _api_error("invalid_existing_identity", 400)
    existing = dict(existing or {})
    if fields["partner_source_id"]:
        if len(fields["formats"]) != 1:
            return None, None, _api_error("ambiguous_existing_partner", 400)
        opposite = "audiobook" if fields["formats"][0] == "ebook" else "ebook"
        existing[opposite] = {
            "source": fields["partner_source"], "source_id": fields["partner_source_id"],
            **({"filename": fields["partner_filename"]} if opposite == "ebook" else {}),
        }
    try:
        job = web.database_service.create_acquisition_job(
            user_id, fields["title"], fields["formats"], authors=", ".join(fields["authors"]) or None,
            isbn13=fields["isbn13"], series_name=fields["series_name"], series_index=fields["series_index"],
            ebook=existing.get("ebook"), audiobook=existing.get("audiobook"), reuse=True,
        )
    except ValueError:
        return None, None, _api_error("invalid_existing_identity", 400)
    except Exception:
        logger.error("Could not save acquisition job before provider submission", exc_info=True)
        return None, None, _api_error("job_save_failed", 503)
    return job, fields, None


def _job_public(job: dict) -> dict:
    """Stable job identity, two independent submission states, and accepted receipts for later UI."""
    return {**job, "requests": [_public(row) for row in job["requests"]]}


def _job_answer(web, user_id: int, job_id: str, **extra):
    job = web.database_service.get_acquisition_job(user_id, job_id)
    return {"job_id": job_id, "job": _job_public(job), "requests": [_public(row) for row in job["requests"]], **extra}


def _bookorbit_request_is_same_edition(remote: dict, fields: dict, kind: str) -> bool:
    """Require positive title, format and supplied edition metadata before adopting a handle."""
    from src.api.bookorbit_client import BookOrbitClient
    normalize = BookOrbitClient._normalize_string
    if not (fields["authors"] or fields["isbn13"] or fields["series_name"]):
        return False
    if remote.get("mediaKind") != kind or not remote.get("title") or (
        normalize(str(remote["title"])) != normalize(fields["title"])
    ):
        return False
    if fields["authors"]:
        authors = remote.get("authors")
        if not authors:
            return False
        authors = authors if isinstance(authors, list) else str(authors).split(",")
        remote_names = [str(value.get("name") if isinstance(value, dict) else value) for value in authors]
        if {normalize(a) for a in remote_names} != {normalize(a) for a in fields["authors"]}:
            return False
    for key, remote_key in (("isbn13", "isbn13"), ("series_name", "seriesName"), ("series_index", "seriesIndex")):
        wanted = fields.get(key)
        if wanted is not None and (
            remote.get(remote_key) is None or
            normalize(str(remote[remote_key])) != normalize(str(wanted))
        ):
            return False
    return True


def acquisition_bookorbit():
    """Save durable intent before BookOrbit I/O; submit only unfinished formats."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _api_error("invalid_json", 400)
    kinds = payload.get("submit_formats", payload.get("formats"))
    if not _valid_submission_formats(kinds):
        return _api_error("invalid_formats", 400)
    client = _bookorbit_client(web)
    if client is None:
        return _api_error("bookorbit_not_configured", 409)
    job, fields, error = _intent(web, user_id, payload, kinds)
    if error:
        return error
    job_id = job["id"]
    work = dict(authors=fields["authors"], isbn13=fields["isbn13"],
                series_name=fields["series_name"], series_index=fields["series_index"])
    current = web.database_service.get_acquisition_job(user_id, job_id)
    already_owned = []
    unfinished = []
    for kind in kinds:
        prefix = "ebook" if kind == "ebook" else "audio"
        if current[f"{prefix}_source_id"] is not None:
            already_owned.append({"format": kind, "book_id": current[f"{prefix}_source_id"]})
        elif not any(r["media_kind"] == kind and r["status"] in (
            "waiting", "in_progress", "needs_look", "arrived", "paired",
        ) for r in current["requests"]):
            unfinished.append(kind)
    if not unfinished:
        return jsonify(_job_answer(web, user_id, job_id, already_owned=already_owned,
                                   failures={}, partial=False))
    items = [{"title": fields["title"], "mediaKind": kind,
              **({"author": fields["authors"][0]} if fields["authors"] else {}),
              **({"isbn13": fields["isbn13"]} if fields["isbn13"] else {})} for kind in unfinished]
    availability = client.check_request_availability(items)
    if not isinstance(availability, list) or len(availability) != len(unfinished):
        return jsonify({"error": "availability_unverified",
                        **_job_answer(web, user_id, job_id)}), 502
    failures = {}
    for kind, available in zip(unfinished, availability):
        current = web.database_service.get_acquisition_job(user_id, job_id)
        prefix = "ebook" if kind == "ebook" else "audio"
        if current[f"{prefix}_source_id"] is not None:
            already_owned.append({"format": kind, "book_id": current[f"{prefix}_source_id"]})
            continue
        active = [r for r in current["requests"] if r["media_kind"] == kind
                  and r["status"] in ("waiting", "in_progress", "needs_look", "arrived", "paired")]
        if active:
            continue
        answer = available if isinstance(available, dict) else {}
        if answer.get("ownedBookId") is not None:
            book_id = answer["ownedBookId"]
            detail = client.get_book_detail(book_id, force=True) if hasattr(client, "get_book_detail") else None
            from src.api.bookorbit_client import BookOrbitClient
            book_file = BookOrbitClient._primary_file(detail, kind=kind) if isinstance(detail, dict) else None
            normalized = BookOrbitClient._normalize_string
            title_matches = (isinstance(detail, dict) and bool(detail.get("title"))
                             and normalized(str(detail["title"])) == normalized(fields["title"]))
            observed_authors = detail.get("authors") if isinstance(detail, dict) else None
            if isinstance(observed_authors, list):
                observed_authors = [str(a.get("name") if isinstance(a, dict) else a) for a in observed_authors]
            else:
                observed_authors = [a.strip() for a in str(observed_authors or "").split(",") if a.strip()]
            author_matches = not fields["authors"] or (
                bool(observed_authors) and {normalized(a) for a in observed_authors}
                == {normalized(a) for a in fields["authors"]}
            )
            detail_series = detail.get("series") if isinstance(detail, dict) else None
            if isinstance(detail_series, list):
                detail_series = next((s for s in detail_series if isinstance(s, dict)), {})
            if not isinstance(detail_series, dict):
                detail_series = {}
            series_name = detail.get("seriesName") or detail_series.get("name") if isinstance(detail, dict) else None
            series_index = detail.get("seriesIndex") if isinstance(detail, dict) else None
            if series_index is None:
                series_index = detail_series.get("position") or detail_series.get("index")
            series_matches = (
                not fields["series_name"] or (
                    normalized(str(series_name or "")) == normalized(fields["series_name"])
                    and (fields["series_index"] is None or str(series_index) == str(fields["series_index"]))
                )
            )
            detail_isbn = detail.get("isbn13") or detail.get("isbn") if isinstance(detail, dict) else None
            isbn_matches = not fields["isbn13"] or (
                str(detail_isbn or "").replace("-", "") == fields["isbn13"].replace("-", "")
            )
            identity_evidence = bool(fields["authors"] or fields["isbn13"] or fields["series_name"])
            if (not identity_evidence or not book_file or (kind == "ebook" and not book_file.get("filename"))
                    or not title_matches or not author_matches or not series_matches or not isbn_matches):
                web.database_service.set_acquisition_submission_state(
                    user_id, job_id, kind, "needs_identification", "owned_edition_unverified",
                )
                failures[kind] = "owned_edition_unverified"
                continue
            identity = {"source": "BookOrbit", "source_id": str(book_id)}
            if kind == "ebook":
                identity["filename"] = book_file["filename"]
            try:
                web.database_service.set_acquisition_job_existing_half(user_id, job_id, kind, identity)
                already_owned.append({"format": kind, "book_id": book_id})
            except ValueError:
                failures[kind] = "owned_identity_conflict"
            continue
        if answer.get("existingRequestId") is not None:
            remote = client.get_request(answer["existingRequestId"])
            if not isinstance(remote, dict) or str(remote.get("id")) != str(answer["existingRequestId"]) or (
                not _bookorbit_request_is_same_edition(remote, fields, kind)
            ):
                web.database_service.set_acquisition_submission_state(
                    user_id, job_id, kind, "uncertain", "existing_request_unverified",
                )
                failures[kind] = "existing_request_unverified"
                continue
        else:
            state = web.database_service.reserve_acquisition_submission(user_id, job_id, kind)
            if state != "reserved":
                if state not in ("already_submitted", "already_owned"):
                    failures[kind] = state
                continue
            # A lost provider response MAY be an accepted request. Persist uncertainty before the POST.
            try:
                remote = _request_item(client.create_request(fields["title"], kind, **work))
            except Exception:
                logger.warning("BookOrbit submission response uncertain for job %s %s", job_id, kind, exc_info=True)
                failures[kind] = "submission_uncertain"
                continue
            if remote.get("id") is None:
                failures[kind] = "submission_uncertain"
                continue
        try:
            changes = AcquisitionService.remote_changes({}, remote)
            web.database_service.add_acquisition_request_to_job(user_id, job_id, {
                "service": "BookOrbit", "media_kind": kind, "remote_kind": "request",
                "remote_id": str(remote["id"]), "status": changes["status"],
                "remote_status": changes["remote_status"], "status_detail": changes["status_detail"],
                "decision_code": changes["decision_code"], "decision_action": changes["decision_action"],
            })
        except Exception:
            logger.warning("Could not attach BookOrbit receipt to acquisition job %s", job_id, exc_info=True)
            failures[kind] = "receipt_unreconciled"
    answer = _job_answer(web, user_id, job_id, already_owned=already_owned, failures=failures,
                         partial=bool(failures))
    if failures and not answer["requests"] and not already_owned:
        return jsonify(answer), 502
    logger.info("📥 Acquisition: user %s job %s BookOrbit formats %s saved (%d receipts)",
                user_id, job_id, kinds, len(answer["requests"]))
    return jsonify(answer)

_SHELFMARK_FAILURES = ("unreachable", "sources_failed", "submit_failed")
_SHELFMARK_CONFIRMED_REFUSALS = frozenset({
    "policy_refused", "release_required", "release_not_allowed", "duplicate_pending_request",
    "bad_credentials", "locked", "password_login_disabled", "no_login_saved",
})


def _usable_shelfmark(web):
    """(user_id, client, None) for a signed-in, permitted reader with Shelfmark set up, else (None, None, error response)."""
    user_id = web.get_current_user_id()
    if user_id is None:
        return None, None, _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return None, None, _api_error("not_allowed", 403)
    client = _shelfmark_client(web)
    if client is None or not client.is_enabled():
        return None, None, _api_error("shelfmark_not_configured", 409)
    return user_id, client, None


def acquisition_shelfmark_releases():
    """Search Shelfmark for releases of one format (slow) and answer a service-neutral list."""
    web = _web_server()
    user_id, client, error = _usable_shelfmark(web)
    if error:
        return error
    kind = request.args.get("format", "")
    title = _text(request.args.get("title"))
    if kind not in _MEDIA_KINDS or not title:
        return _api_error("title_required" if not title else "invalid_formats", 400)
    if hasattr(client, "can_find_releases") and not client.can_find_releases(kind):
        return _api_error("release_not_allowed", 409)
    releases, failure = client.find_releases(title, _text(request.args.get("author")), kind)
    if failure:
        logger.info("📥 Acquisition: user %s Shelfmark release search for a %s failed (%s)", user_id, kind, failure)
        return _api_error(failure, 502 if failure in _SHELFMARK_FAILURES else 409)
    logger.info("📥 Acquisition: user %s Shelfmark release search for a %s found %d release(s)", user_id, kind, len(releases))
    return jsonify({"releases": releases})


def acquisition_shelfmark():
    """Submit one or both Shelfmark halves in order, saving each receipt immediately."""
    web = _web_server()
    user_id, client, error = _usable_shelfmark(web)
    if error:
        return error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _api_error("invalid_json", 400)
    raw_formats = payload.get("submit_formats")
    if raw_formats is None:
        raw_formats = payload.get("formats")
    if raw_formats is None:
        raw_formats = (["ebook", "audiobook"] if payload.get("format") == "both"
                       else [payload["format"]] if "format" in payload else None)
    kinds = raw_formats
    if not _valid_submission_formats(kinds):
        return _api_error("invalid_formats", 400)
    job, fields, error = _intent(web, user_id, payload, kinds)
    if error:
        return error
    job_id = job["id"]
    release_keys = payload.get("release_keys", {})
    if not isinstance(release_keys, dict):
        return _api_error("invalid_release", 400)
    if payload.get("release_key") is not None:
        if len(kinds) != 1:
            return _api_error("invalid_release", 400)
        release_keys = {**release_keys, kinds[0]: payload["release_key"]}
    failures = {}
    needs_release = []
    for kind in ("ebook", "audiobook"):
        if kind not in kinds:
            continue
        current = web.database_service.get_acquisition_job(user_id, job_id)
        prefix = "ebook" if kind == "ebook" else "audio"
        if current[f"{prefix}_source_id"] is not None:
            continue
        if any(row["media_kind"] == kind and row["status"] in (
            "waiting", "in_progress", "needs_look", "arrived", "paired",
        ) for row in current["requests"]):
            continue
        if current[f"{prefix}_submission_state"] == "uncertain":
            failures[kind] = "submission_uncertain"
            continue
        key = release_keys.get(kind)
        if key is not None and (not isinstance(key, str) or not key.strip()):
            failures[kind] = "invalid_release"
            continue
        release = client.release_for(key) if key else None
        if key and release is None:
            failures[kind] = "release_expired"
            continue
        if release and (
            not release.get("source") or not release.get("source_id")
            or client.found_for(key) != kind or release_media_kind(release) not in (None, kind)
        ):
            failures[kind] = "release_wrong_format"
            continue
        if release and hasattr(client, "release_context_for"):
            context = client.release_context_for(key)
            if not context or context[0].strip().casefold() != fields["title"].strip().casefold() or (
                fields["authors"] and context[1] and
                context[1].strip().casefold() != fields["authors"][0].strip().casefold()
            ):
                failures[kind] = "release_wrong_book"
                continue
        source = str(release.get("source") or "") if release else None
        if hasattr(client, "mode_for"):
            mode, policy_error = client.mode_for(kind, source)
        else:
            defaults, policy_error = client.get_policy()
            mode = (defaults or {}).get(kind)
        if policy_error or mode not in ("download", "request_release", "request_book", "blocked"):
            failures[kind] = policy_error or "policy_unverified"
            continue
        if mode == "blocked":
            saved = web.database_service.set_acquisition_submission_state(
                user_id, job_id, kind, "failed", "policy_refused",
            )
            failures[kind] = ("submission_uncertain" if saved and
                              saved[f"{prefix}_submission_state"] == "uncertain" else "policy_refused")
            continue
        if mode in ("download", "request_release") and not release:
            saved = web.database_service.set_acquisition_submission_state(
                user_id, job_id, kind, "needs_release", mode,
            )
            if saved and saved[f"{prefix}_submission_state"] == "needs_release":
                needs_release.append(kind)
            else:
                failures[kind] = "submission_uncertain"
            continue
        if mode == "request_book" and release:
            failures[kind] = "release_not_allowed"
            continue
        state = web.database_service.reserve_acquisition_submission(user_id, job_id, kind)
        if state != "reserved":
            if state not in ("already_submitted", "already_owned"):
                failures[kind] = state
            continue
        try:
            result, failure = client.submit({"title": fields["title"], "authors": fields["authors"]}, release, kind)
        except Exception:
            logger.warning("Shelfmark response uncertain for acquisition job %s %s", job_id, kind, exc_info=True)
            failures[kind] = "submission_uncertain"
            continue
        if failure or not isinstance(result, dict):
            if failure in _SHELFMARK_CONFIRMED_REFUSALS:
                web.database_service.set_acquisition_submission_state(
                    user_id, job_id, kind, "failed", failure[:100], confirmed_rejection=True,
                )
                failures[kind] = failure
            else:
                # No provider idempotency key: generic errors and 5xx may follow acceptance.
                failures[kind] = "submission_uncertain"
            continue
        try:
            receipt = {
                "service": "Shelfmark", "media_kind": kind, "remote_kind": result["kind"],
                "remote_id": result["remote_id"],
                "status": "in_progress" if result["kind"] == "download" else "waiting",
                "remote_status": result.get("status"),
                "decision_code": mode, "decision_action": "wait",
            }
            web.database_service.add_acquisition_request_to_job(user_id, job_id, receipt)
        except Exception:
            logger.warning("Shelfmark receipt for acquisition job %s could not be attached", job_id, exc_info=True)
            failures[kind] = "receipt_unreconciled"
    answer = _job_answer(web, user_id, job_id, failures=failures, needs_release=needs_release,
                         partial=bool(failures or needs_release))
    if len(kinds) == 1:
        matching = next((row for row in answer["job"]["requests"] if row["media_kind"] == kinds[0]), None)
        if matching:
            answer["request"] = matching
            answer["kind"] = matching["remote_kind"]
    if failures and not answer["requests"] and not needs_release:
        status = 502 if any(v in _SHELFMARK_FAILURES or v == "submission_uncertain" for v in failures.values()) else 409
        return jsonify(answer), status
    logger.info("📥 Acquisition: user %s job %s Shelfmark formats %s saved (%d receipts)",
                user_id, job_id, kinds, len(answer["requests"]))
    return jsonify(answer)


def acquisition_mine():
    """The caller's non-dismissed requests, with open ones refreshed from BookOrbit."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    shelfmark = _shelfmark_client(web)
    try:
        library_clients = web._scan_user_clients()
    except PermissionError:
        library_clients = None
    rows = web.container.acquisition_service().refresh_user(
        user_id, _bookorbit_client(web), shelfmark if shelfmark is not None and shelfmark.is_configured() else None,
        library_clients=library_clients,
    )
    jobs = web.database_service.get_acquisition_jobs(user_id)
    tracked = {job["id"]: job for job in jobs}
    for job_id in dict.fromkeys(row.get("group_key") for row in rows if row.get("group_key")):
        if job_id not in tracked:
            job = web.database_service.get_acquisition_job(user_id, str(job_id))
            if job is not None:
                tracked[str(job_id)] = job
    public_rows = []
    for row in rows:
        public = _public(row)
        parent_id = str(row.get("group_key") or "")
        if parent_id in tracked:
            public["parent_job_id"] = parent_id
        public_rows.append(public)
    return jsonify({
        "requests": public_rows,
        "jobs": [_job_public(job) for job in jobs],
        "tracked_jobs": [_job_public(job) for job in tracked.values()],
    })


def acquisition_jobs():
    """List only this reader's saved jobs, including each format's submission decision."""
    web = _web_server()
    uid = web.get_current_user_id()
    if uid is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    return jsonify({"jobs": [_job_public(job) for job in web.database_service.get_acquisition_jobs(uid)]})


def acquisition_job(job_id: str):
    """Return one owned job for the later Processing interface."""
    web = _web_server()
    uid = web.get_current_user_id()
    if uid is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    job = web.database_service.get_acquisition_job(uid, job_id)
    return jsonify({"job": _job_public(job)}) if job else _api_error("not_found", 404)


def _owned_row(web, user_id: int, request_id: int):
    """(row, None) for the caller's own request, else (None, error response)."""
    row = web.database_service.get_acquisition_request(user_id, request_id)
    return (row, None) if row else (None, _api_error("not_found", 404))


def acquisition_cancel(request_id: int):
    """Cancel one of the caller's open requests (a Shelfmark one only while it is still pending or a plain download)."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    row, error = _owned_row(web, user_id, request_id)
    if error:
        return error
    if row["status"] not in OPEN_STATUSES:
        return _api_error("not_open", 409)
    if row["service"] == "Shelfmark":
        client = _shelfmark_client(web)
        if client is None or not client.is_configured():
            return _api_error("shelfmark_not_configured", 409)
        if row["remote_kind"] == "request" and row["remote_status"] != "pending":
            return _api_error("cancel_not_supported", 409)
        cancelled = client.cancel(row["remote_kind"], row["remote_id"])
    else:
        client = _bookorbit_client(web)
        if client is None:
            return _api_error("bookorbit_not_configured", 409)
        cancelled = client.cancel_request(row["remote_id"])
    if not cancelled:
        return _api_error("cancel_failed", 502)
    updated = web.database_service.update_acquisition_request(
        user_id, request_id, status="cancelled", remote_status="cancelled",
    )
    logger.info("📥 Acquisition: user %s cancelled %s request %s", user_id, row["service"], row["remote_id"])
    return jsonify({"request": _public(updated)})


_GRABBED = ("grabbed", "downloading", "importing", "available")
_PICK_FAILURES = {"no_pick_permission": 403, "unreachable": 502}


def _pickable(web, request_id: int):
    """(user_id, row, client, None) for the caller's own open BookOrbit request, else (…, error response)."""
    fail = lambda message, status: (None, None, None, _api_error(message, status))
    user_id = web.get_current_user_id()
    if user_id is None:
        return fail("authentication_required", 401)
    if not _is_allowed(web):
        return fail("not_allowed", 403)
    row = web.database_service.get_acquisition_request(user_id, request_id)
    if row is None or row["service"] != "BookOrbit" or row["remote_kind"] != "request":
        return fail("not_found", 404)
    if row["status"] not in (*OPEN_STATUSES, "failed"):  # BookOrbit lets a failed request be re-picked
        return fail("request_state_changed", 409)
    if row["remote_status"] in _GRABBED:
        return fail("already_picked", 409)
    client = _bookorbit_client(web)
    if client is None:
        return fail("bookorbit_not_configured", 409)
    remote = client.get_request(row["remote_id"])
    if not isinstance(remote, dict) or not remote.get("status"):
        return fail("request_state_unverified", 409)
    if remote.get("id") is not None and str(remote["id"]) != str(row["remote_id"]):
        return fail("request_identity_changed", 409)
    if remote.get("mediaKind") not in (None, row["media_kind"]):
        return fail("request_media_mismatch", 409)
    updates = AcquisitionService.remote_changes(row, remote)
    web.database_service.update_acquisition_request(user_id, request_id, **updates)
    if row["remote_status"] in _GRABBED or remote["status"] in _GRABBED:
        return fail("already_picked", 409)
    reason, action = AcquisitionService.bookorbit_decision(remote)
    if action != "pick_release":
        return fail("configuration_repair_required" if action == "repair_configuration"
                    else "automatic_or_unknown", 409)
    return user_id, row, client, None


def _size_text(size: Any) -> str:
    """Bytes as a short human string; empty when unknown."""
    if not isinstance(size, (int, float)) or isinstance(size, bool) or size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def _release_view(item: dict) -> dict:
    """One BookOrbit release candidate in the service-neutral picker shape."""
    formats = [f for f in item.get("formats") or [] if isinstance(f, str) and f]
    seeders, leechers = item.get("seeders"), item.get("leechers")
    flags = [word for word, on in (("freeleech", item.get("freeleech")), ("VIP", item.get("vipOnly")), ("grabbed before", item.get("alreadyGrabbed"))) if on]
    return {
        "key": f"{item['indexerId']}:{item['guid']}", "title": str(item.get("title") or ""),
        "format": item.get("format") or (formats[0] if formats else ""), "formats_more": max(len(formats) - 1, 0),
        "size": _size_text(item.get("sizeBytes")), "source": item.get("indexerName") or "",
        "language": item.get("language") or "", "detail": "", "author": "", "kind": "",
        "group": str(item["indexerId"]), "group_label": item.get("indexerName") or str(item["indexerId"]),
        "peers": f"{seeders}/{leechers}" if seeders is not None and leechers is not None else "", "flags": flags,
    }


def acquisition_releases(request_id: int):
    """List what BookOrbit's indexers offer for one of the caller's open requests (the picker's list)."""
    web = _web_server()
    user_id, row, client, error = _pickable(web, request_id)
    if error:
        return error
    result, failure = client.list_request_releases(row["remote_id"])
    if failure:
        logger.info("📥 Acquisition: user %s BookOrbit release list for request %s failed (%s)", user_id, row["remote_id"], failure)
        return _api_error(failure, _PICK_FAILURES.get(failure, 409))
    criteria = result.get("criteria") if isinstance(result.get("criteria"), dict) else {}
    if criteria.get("mediaKind") not in (None, row["media_kind"]):
        return _api_error("request_media_mismatch", 409)
    indexers = [i for i in result.get("indexers") or [] if isinstance(i, dict)]
    candidates = [
        r for r in result.get("releases") or []
        if isinstance(r, dict) and isinstance(r.get("indexerId"), int) and isinstance(r.get("guid"), str)
        and release_media_kind({"format": r.get("format"), "extra": {"formats": r.get("formats")}}) in (None, row["media_kind"])
    ]
    releases = [_release_view(r) for r in candidates]
    if indexers and all(str(i.get("delivery")).lower() == "torrent" for i in indexers):
        for release in releases:
            release["kind"] = "torrent"
    problems = [
        {"name": str(i.get("indexerName") or i.get("indexerId")), "failure": str(i.get("failure") or "error")}
        for i in indexers if i.get("ok") is False
    ]
    logger.info(
        "📥 Acquisition: user %s BookOrbit request %s has %d release(s) to pick from (%d indexer problem(s))",
        user_id, row["remote_id"], len(releases), len(problems),
    )
    return jsonify({"releases": releases, "indexer_problems": problems})


def _parse_release_key(key: Any) -> Optional[tuple[int, str]]:
    """(indexer id, guid) from a picker key, or None when malformed."""
    indexer, _, guid = key.partition(":") if isinstance(key, str) else ("", "", "")
    if not indexer.isdigit() or int(indexer) < 1 or not guid or len(guid) > 2048:
        return None
    return int(indexer), guid


def acquisition_grab(request_id: int):
    """Send the picked release to BookOrbit's download client and update the caller's existing row."""
    web = _web_server()
    user_id, row, client, error = _pickable(web, request_id)
    if error:
        return error
    payload = request.get_json(silent=True)
    picked = _parse_release_key(payload.get("key")) if isinstance(payload, dict) else None
    if picked is None:
        return _api_error("invalid_release", 400)
    if row["remote_status"] in _GRABBED:
        return _api_error("already_picked", 409)
    current, failure = client.list_request_releases(row["remote_id"])
    if failure:
        return _api_error(failure, _PICK_FAILURES.get(failure, 409))
    if not isinstance(current, dict) or not any(
        isinstance(item, dict) and item.get("indexerId") == picked[0] and item.get("guid") == picked[1]
        and release_media_kind({"format": item.get("format"), "extra": {"formats": item.get("formats")}})
        in (None, row["media_kind"])
        for item in current.get("releases") or []
    ):
        return _api_error("release_stale", 409)
    item, failure = client.grab_request_release(row["remote_id"], *picked)
    if failure:
        logger.info("📥 Acquisition: user %s BookOrbit refused the release for request %s (%s)", user_id, row["remote_id"], failure)
        if failure == "request_state_changed":
            web.database_service.update_acquisition_request(user_id, request_id, last_checked_at=None)
        return _api_error(failure, _PICK_FAILURES.get(failure, 409))
    changes = AcquisitionService.remote_changes(row, {**item, "status": item.get("status") or "grabbed"})
    updated = web.database_service.update_acquisition_request(user_id, request_id, last_checked_at=None, **changes)
    logger.info("📥 Acquisition: user %s picked a release for BookOrbit request %s", user_id, row["remote_id"])
    return jsonify({"request": _public(updated)})


def acquisition_dismiss(request_id: int):
    """Hide one of the caller's requests from the My requests strip."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    _, error = _owned_row(web, user_id, request_id)
    if error:
        return error
    web.database_service.update_acquisition_request(user_id, request_id, dismissed=True)
    return jsonify({"ok": True})


def _owned_job(web: Any, user_id: int, job_id: str) -> tuple[Optional[dict], Optional[Any]]:
    """Return a reader-owned saved job, including dismissed history, or a 404 response."""
    job = web.database_service.get_acquisition_job(user_id, job_id)
    return (job, None) if job else (None, _api_error("not_found", 404))


def acquisition_job_dismiss(job_id: str) -> Any:
    """Hide one owned acquisition job without cancelling or deleting its receipts."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    job, error = _owned_job(web, user_id, job_id)
    if error:
        return error
    if job["dismissed"]:
        return jsonify({"ok": True, "job": _job_public(job)})
    if not web.database_service.dismiss_acquisition_job(user_id, job_id):
        return _api_error("not_found", 404)
    return jsonify({"ok": True, "job": _job_public(web.database_service.get_acquisition_job(user_id, job_id))})


def acquisition_job_review(job_id: str, media_kind: str) -> Any:
    """Confirm one offered library candidate only after a fresh reader-scoped lookup."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    if media_kind not in _MEDIA_KINDS:
        return _api_error("invalid_format", 400)
    _, error = _owned_job(web, user_id, job_id)
    if error:
        return error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _api_error("invalid_json", 400)
    selection = {
        "source": payload.get("source"),
        "source_id": payload.get("source_id"),
    }
    if media_kind == "ebook":
        selection["filename"] = payload.get("filename")
    try:
        clients = web._scan_user_clients()
    except PermissionError:
        return _api_error("reader_library_unavailable", 409)
    saved, failure = web.container.acquisition_service().choose_review_candidate(
        user_id, job_id, media_kind, selection, clients,
    )
    if failure:
        code = 503 if failure == "catalog_unavailable" else 409
        return _api_error(failure, code)
    return jsonify({"job": _job_public(saved)})


def acquisition_job_scan_retry(job_id: str) -> Any:
    """Retry a settled Shelfmark audio scan only after live operation verification."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    _, error = _owned_job(web, user_id, job_id)
    if error:
        return error
    try:
        clients = web._scan_user_clients()
    except PermissionError:
        return _api_error("reader_library_unavailable", 409)
    job, failure = web.container.acquisition_service().retry_acquisition_scan(
        user_id, job_id, clients,
    )
    if failure:
        status = 502 if failure in ("scan_history_unavailable", "unreachable", "invalid_scan_history") else 409
        return jsonify({"error": failure, "job": _job_public(job) if job else None}), status
    return jsonify({"job": _job_public(job)})
