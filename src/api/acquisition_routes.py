"""User-scoped API for asking BookOrbit or Shelfmark to fetch a book a search did not find."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from flask import current_app, jsonify, request

from src.services.acquisition_service import OPEN_STATUSES, STATUS_MAP
from src.utils.config_loader import env_truthy

logger = logging.getLogger(__name__)

_MEDIA_KINDS = ("ebook", "audiobook")
_PUBLIC_FIELDS = (
    "id", "service", "media_kind", "remote_kind", "title", "authors", "status", "status_detail", "remote_status",
    "partner_source", "partner_source_id", "group_key", "arrived_source_id", "linked_abs_id",
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
    return {key: row[key] for key in _PUBLIC_FIELDS}


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
                service.update(usable=True, formats=list(_MEDIA_KINDS))
    return jsonify({"services": {"bookorbit": service, "shelfmark": _shelfmark_service(web)}})


def _shelfmark_service(web) -> dict:
    """Whether Shelfmark is usable for this reader and in which mode per format (one policy call)."""
    service = {"usable": False, "reason": None, "formats": {}}
    client = _shelfmark_client(web)
    if not _is_allowed(web):
        service["reason"] = "toggle_off"
    elif client is None or not client.is_enabled():
        service["reason"] = "shelfmark_not_configured"
    else:
        modes, error = client.get_policy()
        formats = {kind: mode for kind, mode in (modes or {}).items() if mode != "blocked"}
        if modes is None:
            service["reason"] = error
        elif not formats:
            service["reason"] = "policy_blocked"
        else:
            service.update(usable=True, formats=formats)
    return service


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
        try:
            series_index = int(series_index)
        except (TypeError, ValueError):
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


def _already_owned(client, fields: dict) -> dict[str, Any]:
    """{format: BookOrbit book id} for formats BookOrbit already holds."""
    items = []
    for kind in fields["formats"]:
        item = {"title": fields["title"], "mediaKind": kind}
        if fields["authors"]:
            item["author"] = fields["authors"][0]
        if fields["isbn13"]:
            item["isbn13"] = fields["isbn13"]
        items.append(item)
    answers = client.check_request_availability(items) or []
    return {
        kind: answer["ownedBookId"]
        for kind, answer in zip(fields["formats"], answers)
        if isinstance(answer, dict) and answer.get("ownedBookId") is not None
    }


def _file_requests(client, fields: dict, kinds: list[str]) -> dict[str, dict]:
    """File the request(s) with BookOrbit; {format: request object} for those it accepted."""
    work = dict(
        authors=fields["authors"], isbn13=fields["isbn13"],
        series_name=fields["series_name"], series_index=fields["series_index"],
    )
    if len(kinds) == 2:
        answer = client.create_request_pair(fields["title"], **work) or {}
        results = {kind: answer.get(kind) for kind in kinds}
    else:
        results = {kinds[0]: client.create_request(fields["title"], kinds[0], **work)}
    items = {kind: _request_item(result) for kind, result in results.items()}
    return {kind: item for kind, item in items.items() if item.get("id") is not None}


def _save_row(web, user_id: int, row: dict) -> dict:
    """Save a request row, or return the user's existing row for the same remote id (keeping its partner)."""
    existing = web.database_service.find_acquisition_request(user_id, row["service"], row["remote_id"])
    if existing is None:
        return web.database_service.add_acquisition_requests(user_id, [row])[0]
    if not existing["partner_source_id"] and row["partner_source_id"]:
        existing = web.database_service.update_acquisition_request(
            user_id, existing["id"], partner_source=row["partner_source"],
            partner_source_id=row["partner_source_id"], partner_filename=row["partner_filename"],
        )
    return existing


def acquisition_bookorbit():
    """Ask BookOrbit for one format or both, and save a row per format filed."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    payload = request.get_json(silent=True)
    fields, error = _parse_create_body(payload) if isinstance(payload, dict) else (None, "invalid_json")
    if error:
        return _api_error(error, 400)
    client = _bookorbit_client(web)
    if client is None:
        return _api_error("bookorbit_not_configured", 409)

    owned = _already_owned(client, fields)
    kinds = [kind for kind in fields["formats"] if kind not in owned]
    owned_list = [{"format": kind, "book_id": book_id} for kind, book_id in owned.items()]
    if owned:
        logger.info(
            "📥 Acquisition: user %s asked for formats %s; BookOrbit already owns %s",
            user_id, fields["formats"], sorted(owned),
        )
    if not kinds:
        return jsonify({"requests": [], "already_owned": owned_list})

    filed = _file_requests(client, fields, kinds)
    if not filed:
        return _api_error("request_failed", 502)
    group_key = uuid.uuid4().hex if len(filed) == 2 else None
    rows = [
        {
            "service": "BookOrbit", "media_kind": kind, "remote_kind": "request",
            "remote_id": str(item["id"]), "title": fields["title"],
            "authors": ", ".join(fields["authors"]) or None, "isbn13": fields["isbn13"],
            "series_name": fields["series_name"], "series_index": fields["series_index"],
            "status": STATUS_MAP.get(item.get("status"), "waiting"),
            "remote_status": item.get("status"),
            "partner_source": fields["partner_source"],
            "partner_source_id": fields["partner_source_id"],
            "partner_filename": fields["partner_filename"], "group_key": group_key,
        }
        for kind, item in filed.items()
    ]
    saved = [_save_row(web, user_id, row) for row in rows]
    logger.info(
        "📥 Acquisition: user %s filed BookOrbit request(s) %s for formats %s",
        user_id, [row["remote_id"] for row in saved], sorted(filed),
    )
    return jsonify({
        "requests": [_public(row) for row in saved],
        "already_owned": owned_list,
        "partial": len(filed) < len(kinds),
    })


_SHELFMARK_FAILURES = ("unreachable", "sources_failed", "submit_failed")


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
    releases, failure = client.find_releases(title, _text(request.args.get("author")), kind)
    if failure:
        logger.info("📥 Acquisition: user %s Shelfmark release search for a %s failed (%s)", user_id, kind, failure)
        return _api_error(failure, 502 if failure in _SHELFMARK_FAILURES else 409)
    logger.info("📥 Acquisition: user %s Shelfmark release search for a %s found %d release(s)", user_id, kind, len(releases))
    return jsonify({"releases": releases})


def acquisition_shelfmark():
    """Download the chosen Shelfmark release, or file a book-level request, and save one row."""
    web = _web_server()
    user_id, client, error = _usable_shelfmark(web)
    if error:
        return error
    payload = request.get_json(silent=True)
    fields, invalid = _parse_create_body({**payload, "formats": [payload.get("format")]}) if isinstance(payload, dict) else (None, "invalid_json")
    if invalid:
        return _api_error(invalid, 400)
    kind = fields["formats"][0]
    release = None
    if payload.get("release_key"):
        release = client.release_for(str(payload["release_key"]))
        if release is None:
            return _api_error("release_expired", 409)
    result, failure = client.submit({"title": fields["title"], "authors": fields["authors"]}, release, kind)
    if failure:
        logger.info("📥 Acquisition: user %s Shelfmark did not accept the %s (%s)", user_id, kind, failure)
        return _api_error(failure, 502 if failure in _SHELFMARK_FAILURES else 409)
    is_download = result["kind"] == "download"
    row = _save_row(web, user_id, {
        "service": "Shelfmark", "media_kind": kind, "remote_kind": result["kind"], "remote_id": result["remote_id"],
        "title": fields["title"], "authors": ", ".join(fields["authors"]) or None, "isbn13": fields["isbn13"],
        "series_name": fields["series_name"], "series_index": fields["series_index"],
        "status": "in_progress" if is_download else "waiting", "remote_status": result["status"],
        "partner_source": fields["partner_source"], "partner_source_id": fields["partner_source_id"],
        "partner_filename": fields["partner_filename"],
    })
    logger.info("📥 Acquisition: user %s Shelfmark %s %s for a %s", user_id, "download queued as" if is_download else "request filed as", result["remote_id"], kind)
    return jsonify({"kind": result["kind"], "request": _public(row)})


def acquisition_mine():
    """The caller's non-dismissed requests, with open ones refreshed from BookOrbit."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None:
        return _api_error("authentication_required", 401)
    if not _is_allowed(web):
        return _api_error("not_allowed", 403)
    shelfmark = _shelfmark_client(web)
    rows = web.container.acquisition_service().refresh_user(
        user_id, _bookorbit_client(web), shelfmark if shelfmark is not None and shelfmark.is_configured() else None,
    )
    return jsonify({"requests": [_public(row) for row in rows]})


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
