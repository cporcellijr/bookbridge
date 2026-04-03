# KoSync Server - Extracted from web_server.py for clean code separation
# Implements KOSync protocol compatible with kosync-dotnet
import logging
import mimetypes
import os
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from flask import Blueprint, jsonify, request, send_file

from src.utils.kosync_headers import hash_kosync_key

logger = logging.getLogger(__name__)

# Create Blueprints for KoSync endpoints
# kosync_sync_bp: KOReader protocol routes (safe to expose to internet)
# kosync_admin_bp: Dashboard management routes (LAN only)
kosync_sync_bp = Blueprint('kosync', __name__)
kosync_admin_bp = Blueprint('kosync_admin', __name__)

# Module-level references - set via init_kosync_server()
_database_service = None
_container = None
_manager = None
_hash_cache = None
_ebook_dir = None
_active_scans = set()

# KoSync PUT debounce state
_kosync_debounce: dict = {}  # {abs_id: {'last_event': float, 'title': str, 'synced': bool}}
_kosync_debounce_lock = threading.Lock()
_debounce_thread_started = False
_kosync_open_sessions: dict = {}  # {session_key: session_dict}
_kosync_open_sessions_lock = threading.Lock()
_KOSYNC_SESSION_GAP_SECONDS = 300
_KOSYNC_SESSION_MIN_SECONDS = 30
_KOSYNC_SESSION_MAX_SECONDS = 7200
_KOSYNC_DEVICE_SESSION_REGISTRY_KEY = "KOSYNC_DEVICE_SESSION_REGISTRY"
_kosync_device_session_registry = None
_kosync_device_session_registry_lock = threading.Lock()

def init_kosync_server(database_service, container, manager, ebook_dir=None):
    """Initialize KoSync server with required dependencies."""
    global _database_service, _container, _manager, _ebook_dir, _kosync_device_session_registry
    _database_service = database_service
    _container = container
    _manager = manager
    _ebook_dir = ebook_dir
    _kosync_device_session_registry = None


def _get_koreader_device_sync_service():
    if not _container:
        return None
    try:
        return _container.koreader_device_sync_service()
    except Exception as e:
        logger.warning(f"KOReader device-sync service unavailable: {e}")
        return None


def _record_kosync_event(abs_id: str, title: str) -> None:
    """Record a KoSync PUT event for debounced sync triggering."""
    global _debounce_thread_started
    with _kosync_debounce_lock:
        _kosync_debounce[abs_id] = {
            'last_event': time.time(),
            'title': title,
            'synced': False,
        }
    if not _debounce_thread_started:
        _debounce_thread_started = True
        threading.Thread(target=_kosync_debounce_loop, daemon=True).start()


def _normalize_kosync_device_value(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_internal_kosync_device(device: str | None, device_id: str | None = None) -> bool:
    normalized_device = _normalize_kosync_device_value(device)
    normalized_device_id = _normalize_kosync_device_value(device_id)
    return normalized_device in ("abs-sync-bot", "abs-kosync-bridge") or normalized_device_id in (
        "abs-sync-bot",
        "abs-kosync-bridge",
    )


def _get_kosync_device_key(device: str | None, device_id: str | None) -> str:
    normalized_device_id = (device_id or "").strip()
    if normalized_device_id:
        return normalized_device_id
    return _normalize_kosync_device_value(device)


def _load_kosync_device_session_registry() -> dict:
    global _kosync_device_session_registry
    with _kosync_device_session_registry_lock:
        if _kosync_device_session_registry is None:
            if not _database_service:
                _kosync_device_session_registry = {}
            else:
                loaded = _database_service.get_json_setting(_KOSYNC_DEVICE_SESSION_REGISTRY_KEY, default={})
                _kosync_device_session_registry = loaded if isinstance(loaded, dict) else {}
        return dict(_kosync_device_session_registry)


def _save_kosync_device_session_registry(registry: dict) -> None:
    global _kosync_device_session_registry
    with _kosync_device_session_registry_lock:
        _kosync_device_session_registry = dict(registry)
        if _database_service:
            _database_service.set_json_setting(_KOSYNC_DEVICE_SESSION_REGISTRY_KEY, _kosync_device_session_registry)


def _get_kosync_device_session_entry(device: str | None, device_id: str | None) -> dict | None:
    device_key = _get_kosync_device_key(device, device_id)
    if not device_key:
        return None
    registry = _load_kosync_device_session_registry()
    entry = registry.get(device_key)
    return dict(entry) if isinstance(entry, dict) else None


def _upsert_kosync_device_session_entry(
    device: str | None,
    device_id: str | None,
    mode: str,
    source: str,
    last_document_hash: str | None = None,
    seen_time: datetime | None = None,
) -> bool:
    device_key = _get_kosync_device_key(device, device_id)
    if not device_key:
        return False

    now_iso = (seen_time or datetime.utcnow()).isoformat() + "Z"
    registry = _load_kosync_device_session_registry()
    existing = registry.get(device_key) if isinstance(registry.get(device_key), dict) else {}
    first_seen = existing.get("first_seen") or now_iso

    registry[device_key] = {
        "mode": mode,
        "device": device or existing.get("device"),
        "device_id": device_id or existing.get("device_id"),
        "source": source,
        "first_seen": first_seen,
        "last_seen": now_iso,
        "last_document_hash": last_document_hash or existing.get("last_document_hash"),
    }
    _save_kosync_device_session_registry(registry)
    return True


def _supports_estimated_kosync_sessions(device: str | None, device_id: str | None) -> bool:
    """Estimate KOSync sessions for any classified-or-unclassified non-plugin device."""
    device_key = _get_kosync_device_key(device, device_id)
    if not device_key:
        return False

    entry = _get_kosync_device_session_entry(device, device_id)
    if not entry:
        return True

    mode = (entry.get("mode") or "").strip().lower()
    if mode in ("plugin", "ignored"):
        return False
    return True


def _get_kosync_session_key(doc_hash: str, device: str, device_id: str) -> str:
    device_key = _get_kosync_device_key(device, device_id) or "unknown"
    return f"{doc_hash}::{device_key}"


def _get_kosync_session_type(book) -> str:
    ebook_filename = (getattr(book, "ebook_filename", None) or "").lower()
    if ebook_filename.endswith(".epub"):
        return "EPUB"
    if ebook_filename.endswith(".pdf"):
        return "PDF"
    return "EBOOK"


def _persist_grouped_kosync_session(session_data: dict) -> None:
    if not _supports_estimated_kosync_sessions(session_data["device"], session_data["device_id"]):
        logger.debug(
            "KOSync estimated session skipped for '%s' (device '%s' classified as plugin/ignored)",
            session_data["title"],
            session_data["device"] or "unknown",
        )
        return

    duration_seconds = int(session_data["last_time"] - session_data["start_time"])
    if duration_seconds < _KOSYNC_SESSION_MIN_SECONDS or duration_seconds > _KOSYNC_SESSION_MAX_SECONDS:
        return

    _database_service.record_reading_session(
        abs_id=session_data["abs_id"],
        session_type=session_data["session_type"],
        start_time=session_data["start_time"],
        end_time=session_data["last_time"],
        duration_seconds=duration_seconds,
        start_progress=session_data["start_progress"],
        end_progress=session_data["last_progress"],
        leader_client=f"KoSync:{session_data['device'] or 'unknown'}",
    )
    logger.info(
        "KOSync session recorded for '%s' from %s: %.2f%% -> %.2f%% over %ss",
        session_data["title"],
        session_data["device"] or "unknown",
        float(session_data["start_progress"]) * 100.0,
        float(session_data["last_progress"]) * 100.0,
        duration_seconds,
    )

    if (
        os.environ.get("GRIMMORY_READING_SESSIONS", "true").lower() == "true"
        and _manager
        and hasattr(_manager, "booklore_client")
        and _manager.booklore_client
        and _manager.booklore_client.is_configured()
        and hasattr(_manager, "_resolve_grimmory_ebook_id")
    ):
        try:
            book = _database_service.get_book(session_data["abs_id"])
            grimmory_id = _manager._resolve_grimmory_ebook_id(book) if book else None
            if grimmory_id:
                _manager.booklore_client.create_reading_session(
                    book_id=int(grimmory_id),
                    start_time=session_data["start_time"],
                    end_time=session_data["last_time"],
                    start_progress=session_data["start_progress"],
                    end_progress=session_data["last_progress"],
                    book_type=session_data["session_type"],
                )
        except Exception as e:
            logger.warning("KOSync session forwarding failed for '%s': %s", session_data["abs_id"], e)


def _discard_open_kosync_session(document_hash: str | None, device: str | None, device_id: str | None) -> bool:
    if not document_hash:
        return False
    session_key = _get_kosync_session_key(document_hash, device, device_id)
    with _kosync_open_sessions_lock:
        removed = _kosync_open_sessions.pop(session_key, None) is not None
    logger.debug(
        "_discard_open_kosync_session: key='%s' found=%s",
        session_key,
        removed,
    )
    return removed


def _discard_open_kosync_sessions_for_book(abs_id: str) -> list[dict]:
    """Remove and return all open estimated sessions for a given book, matched by abs_id.

    Used as a fallback when the document-hash/device key-based discard cannot resolve
    the session key (e.g. because the KosyncDocument has no device info populated).
    """
    with _kosync_open_sessions_lock:
        keys = [k for k, v in _kosync_open_sessions.items() if v.get("abs_id") == abs_id]
        removed = [_kosync_open_sessions.pop(k) for k in keys]
    if removed:
        logger.debug(
            "_discard_open_kosync_sessions_for_book: abs_id='%s' discarded %d session(s): %s",
            abs_id,
            len(removed),
            [s.get("device") or "unknown" for s in removed],
        )
    return removed


def _flush_stale_kosync_sessions(now_ts: float | None = None) -> None:
    if not _database_service:
        return

    current_ts = now_ts if now_ts is not None else time.time()
    stale_sessions = []
    with _kosync_open_sessions_lock:
        stale_keys = [
            key for key, data in _kosync_open_sessions.items()
            if (current_ts - data["last_time"]) > _KOSYNC_SESSION_GAP_SECONDS
        ]
        for key in stale_keys:
            stale_sessions.append(_kosync_open_sessions.pop(key))

    for session_data in stale_sessions:
        logger.debug(
            "KOSync session stale-finalize for doc %s from %s: %.2f%% -> %.2f%% over %ss",
            session_data["document_hash"],
            session_data["device"] or "unknown",
            float(session_data["start_progress"]) * 100.0,
            float(session_data["last_progress"]) * 100.0,
            int(session_data["last_time"] - session_data["start_time"]),
        )
        _persist_grouped_kosync_session(session_data)


def _update_grouped_kosync_session(book, doc_hash: str, device: str, device_id: str, percentage, now_ts: float) -> None:
    if not _database_service or not book or not _supports_estimated_kosync_sessions(device, device_id):
        return

    global _debounce_thread_started
    current_progress = float(percentage or 0.0)
    session_key = _get_kosync_session_key(doc_hash, device, device_id)
    new_session = {
        "abs_id": book.abs_id,
        "title": getattr(book, "abs_title", book.abs_id),
        "document_hash": doc_hash,
        "device": device,
        "device_id": device_id,
        "session_type": _get_kosync_session_type(book),
        "start_time": now_ts,
        "last_time": now_ts,
        "start_progress": current_progress,
        "last_progress": current_progress,
    }

    session_to_finalize = None
    with _kosync_open_sessions_lock:
        existing = _kosync_open_sessions.get(session_key)
        if not existing:
            _kosync_open_sessions[session_key] = new_session
            logger.debug(
                "KOSync session opened for doc %s from %s at %.2f%%",
                doc_hash,
                device or "unknown",
                current_progress * 100.0,
            )
            existing = None
        elif existing["abs_id"] != book.abs_id or (now_ts - existing["last_time"]) > _KOSYNC_SESSION_GAP_SECONDS:
            session_to_finalize = existing
            _kosync_open_sessions[session_key] = new_session
            logger.debug(
                "KOSync session split for doc %s from %s after gap/book change: new start %.2f%%",
                doc_hash,
                device or "unknown",
                current_progress * 100.0,
            )
        elif current_progress > float(existing["last_progress"]) + 0.0001:
            existing["last_time"] = now_ts
            existing["last_progress"] = current_progress
            logger.debug(
                "KOSync session extended for doc %s from %s to %.2f%%",
                doc_hash,
                device or "unknown",
                current_progress * 100.0,
            )
        else:
            logger.debug(
                "KOSync session heartbeat ignored for doc %s from %s at %.2f%% (last forward %.2f%%)",
                doc_hash,
                device or "unknown",
                current_progress * 100.0,
                float(existing["last_progress"]) * 100.0,
            )

    if session_to_finalize:
        _persist_grouped_kosync_session(session_to_finalize)

    if not _debounce_thread_started:
        _debounce_thread_started = True
        threading.Thread(target=_kosync_debounce_loop, daemon=True).start()


def _kosync_debounce_loop() -> None:
    """Check every 10s for books that stopped receiving KoSync PUTs."""
    debounce_seconds = int(os.environ.get('ABS_SOCKET_DEBOUNCE_SECONDS', '30'))
    while True:
        time.sleep(10)
        now = time.time()
        _flush_stale_kosync_sessions(now)
        to_sync = []

        with _kosync_debounce_lock:
            for abs_id, info in _kosync_debounce.items():
                if not info['synced'] and (now - info['last_event']) > debounce_seconds:
                    info['synced'] = True
                    to_sync.append((abs_id, info['title']))

        for abs_id, title in to_sync:
            if _manager:
                logger.info(f"⚡ KOSync PUT: Triggering sync for '{title}' (debounced)")
                threading.Thread(
                    target=_manager.sync_cycle,
                    kwargs={'target_abs_id': abs_id},
                    daemon=True,
                ).start()

        # Clean up entries older than 5 minutes
        with _kosync_debounce_lock:
            stale = [k for k, v in _kosync_debounce.items() if now - v['last_event'] > 300]
            for k in stale:
                del _kosync_debounce[k]


def kosync_auth_required(f):
    """Decorator for KOSync authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = request.headers.get('x-auth-user')
        key = request.headers.get('x-auth-key')

        expected_user = os.environ.get("KOSYNC_USER")
        expected_password = os.environ.get("KOSYNC_KEY")

        if not expected_user or not expected_password:
            logger.error(f"❌ KOSync Integrated Server: Credentials not configured in settings (request from {request.remote_addr})")
            return jsonify({"error": "Server not configured"}), 500

        expected_hash = hash_kosync_key(expected_password)

        if user and expected_user and user.lower() == expected_user.lower() and (key == expected_password or key == expected_hash):
            return f(*args, **kwargs)

        logger.warning(f"⚠️ KOSync Integrated Server: Unauthorized access attempt from '{request.remote_addr}' (user: '{user}')")
        return jsonify({"error": "Unauthorized"}), 401
    return decorated_function


# ---------------- KOSync Protocol Endpoints ----------------

@kosync_sync_bp.route('/healthcheck')
@kosync_sync_bp.route('/koreader/healthcheck')
def kosync_healthcheck():
    """KOSync connectivity check"""
    return "OK", 200


@kosync_sync_bp.route('/users/auth', methods=['GET'])
@kosync_sync_bp.route('/koreader/users/auth', methods=['GET'])
def kosync_users_auth():
    """KOReader auth check - validates credentials per kosync-dotnet spec"""
    user = request.headers.get('x-auth-user')
    key = request.headers.get('x-auth-key')

    expected_user = os.environ.get("KOSYNC_USER")
    expected_password = os.environ.get("KOSYNC_KEY")

    if not user or not key:
        logger.warning(f"⚠️ KOSync Auth: Missing credentials from '{request.remote_addr}'")
        return jsonify({"message": "Invalid credentials"}), 401

    if not expected_user or not expected_password:
        logger.error("❌ KOSync Auth: Server credentials not configured")
        return jsonify({"message": "Server not configured"}), 500

    expected_hash = hash_kosync_key(expected_password)

    if user.lower() == expected_user.lower() and (key == expected_password or key == expected_hash):
        logger.debug(f"KOSync Auth: User '{user}' authenticated successfully")
        return jsonify({"username": user}), 200

    logger.warning(f"⚠️ KOSync Auth: Failed auth attempt for user '{user}' from '{request.remote_addr}'")
    return jsonify({"message": "Unauthorized"}), 401


@kosync_sync_bp.route('/users/create', methods=['POST'])
@kosync_sync_bp.route('/koreader/users/create', methods=['POST'])
def kosync_users_create():
    """Stub for KOReader user registration check"""
    return jsonify({
        "id": 1,
        "username": os.environ.get("KOSYNC_USER", "user")
    }), 201


@kosync_sync_bp.route('/users/login', methods=['POST'])
@kosync_sync_bp.route('/koreader/users/login', methods=['POST'])
def kosync_users_login():
    """Stub for KOReader login check"""
    return jsonify({
        "id": 1,
        "username": os.environ.get("KOSYNC_USER", "user"),
        "active": True,
        "token": os.environ.get("KOSYNC_KEY", "")
    }), 200


@kosync_sync_bp.route('/syncs/progress/<doc_id>', methods=['GET'])
@kosync_sync_bp.route('/koreader/syncs/progress/<doc_id>', methods=['GET'])
@kosync_auth_required
def kosync_get_progress(doc_id):
    """
    Fetch progress for a specific document.
    Returns 502 (not 404) if document not found, per kosync-dotnet spec.

    Lookup order:
      1. Direct hash match in kosync_documents
      2. Book lookup by kosync_doc_id
      3. Sibling hash resolution (same book, different epub hash)
      4. Background auto-discovery for completely unknown hashes
    """
    logger.info(f"KOSync: GET progress for doc {doc_id} from {request.remote_addr}")

    # Step 1: Direct hash lookup
    kosync_doc = _database_service.get_kosync_document(doc_id)
    if kosync_doc:
        # If linked to a book, always check siblings for freshest progress.
        # This prevents "shadow" docs (created by sync-bot PUTs) from returning
        # stale data when the real device hash has advanced further.
        if kosync_doc.linked_abs_id:
            book = _database_service.get_book(kosync_doc.linked_abs_id)
            if book:
                return _respond_from_book_states(doc_id, book)

        has_progress = kosync_doc.percentage and float(kosync_doc.percentage) > 0
        if has_progress:
            poison_pill = _suppress_empty_progress_response(
                doc_id,
                float(kosync_doc.percentage),
                kosync_doc.progress,
            )
            if poison_pill is not None:
                return poison_pill
            return jsonify({
                "device": kosync_doc.device or "",
                "device_id": kosync_doc.device_id or "",
                "document": kosync_doc.document_hash,
                "percentage": float(kosync_doc.percentage) if kosync_doc.percentage else 0,
                "progress": kosync_doc.progress or "",
                "timestamp": int(kosync_doc.timestamp.timestamp()) if kosync_doc.timestamp else 0
            }), 200
        # Document exists but has no progress and no linked book — fall through
        # to try sibling resolution for better data

    # Step 2: Book lookup by kosync_doc_id
    book = _database_service.get_book_by_kosync_id(doc_id)
    if book:
        return _respond_from_book_states(doc_id, book)

    # Step 3: Sibling hash resolution — find the book via other linked hashes
    resolved_book = _resolve_book_by_sibling_hash(doc_id, existing_doc=kosync_doc)
    if resolved_book:
        _register_hash_for_book(doc_id, resolved_book)
        return _respond_from_book_states(doc_id, resolved_book)

    # Step 4: Unknown hash — register stub and start background discovery
    auto_create = os.environ.get('AUTO_CREATE_EBOOK_MAPPING', 'true').lower() == 'true'
    if auto_create and doc_id not in _active_scans:
        _active_scans.add(doc_id)
        from src.db.models import KosyncDocument as KD
        stub = KD(document_hash=doc_id)
        _database_service.save_kosync_document(stub)
        logger.info(f"🔍 KOSync: Created stub for unknown hash {doc_id}, starting background discovery")
        threading.Thread(target=_run_get_auto_discovery, args=(doc_id,), daemon=True).start()

    logger.warning(f"⚠️ KOSync: Document not found: {doc_id} (GET from {request.remote_addr})")
    return jsonify({"message": "Document not found on server"}), 502


@kosync_sync_bp.route('/syncs/progress', methods=['PUT'])
@kosync_sync_bp.route('/koreader/syncs/progress', methods=['PUT'])
@kosync_auth_required
def kosync_put_progress():
    """
    Receive progress update from KOReader.
    Stores ALL documents, whether mapped to ABS or not.
    """
    from flask import current_app
    from src.db.models import KosyncDocument, Book

    data = request.json
    if not data:
        logger.warning(f"KOSync: PUT progress with no JSON data from {request.remote_addr}")
        return jsonify({"error": "No data"}), 400

    doc_hash = data.get('document')
    if not doc_hash:
        logger.warning(f"KOSync: PUT progress with no document ID from {request.remote_addr}")
        return jsonify({"error": "Missing document ID"}), 400

    logger.info(f"KOSync: PUT progress request for doc {doc_hash} from {request.remote_addr} (device: {data.get('device', 'unknown')})")

    percentage = data.get('percentage', 0)
    progress = data.get('progress', '')
    device = data.get('device', '')
    device_id = data.get('device_id', '')
    now = datetime.utcnow()
    now_ts = time.time()
    _flush_stale_kosync_sessions(now_ts)

    kosync_doc = _database_service.get_kosync_document(doc_hash)

    # Optional "furthest wins" protection
    furthest_wins = os.environ.get('KOSYNC_FURTHEST_WINS', 'true').lower() == 'true'
    force_update = data.get('force', False)
    is_internal = _is_internal_kosync_device(device, device_id)

    # Allow rewinds if:
    # 1. Force flag is set (e.g. from SyncManager)
    # 2. Update comes from the SAME device (user moved slider back)
    # 3. Update is internal (sync-bot) — must reach debounce-clear logic below
    same_device = (kosync_doc and kosync_doc.device_id == device_id)

    if furthest_wins and kosync_doc and kosync_doc.percentage and not force_update and not same_device and not is_internal:
        existing_pct = float(kosync_doc.percentage)
        new_pct = float(percentage)

        if new_pct < existing_pct - 0.0001:
            logger.info(f"KOSync: Ignored progress from '{device}' for doc {doc_hash} (server has higher: {existing_pct:.2f}% vs new {new_pct:.2f}%)")
            return jsonify({
                "document": doc_hash,
                "timestamp": int(kosync_doc.timestamp.timestamp()) if kosync_doc.timestamp else int(now.timestamp())
            }), 200

    if kosync_doc is None:
        kosync_doc = KosyncDocument(
            document_hash=doc_hash,
            progress=progress,
            percentage=percentage,
            device=device,
            device_id=device_id,
            timestamp=now
        )
        logger.info(f"KOSync: New document tracked: {doc_hash} from device '{device}'")
    else:
        logger.info(
            f"KOSync: Received progress from '{device}' for doc {doc_hash} -> "
            f"{float(percentage):.2%} (Updated from {float(kosync_doc.percentage) if kosync_doc.percentage else 0:.2%})"
        )
        existing_device = kosync_doc.device
        existing_device_id = kosync_doc.device_id
        kosync_doc.progress = progress
        kosync_doc.percentage = percentage
        if is_internal and not _is_internal_kosync_device(existing_device, existing_device_id):
            logger.debug(
                "KOSync: Preserving external device identity '%s' (%s) for doc %s during internal update from '%s'",
                existing_device or "unknown",
                existing_device_id or "no-device-id",
                doc_hash,
                device or "unknown",
            )
        else:
            kosync_doc.device = device
            kosync_doc.device_id = device_id
        kosync_doc.timestamp = now

    _database_service.save_kosync_document(kosync_doc)

    # Update linked book if exists
    linked_book = None
    if kosync_doc.linked_abs_id:
        linked_book = _database_service.get_book(kosync_doc.linked_abs_id)
    else:
        linked_book = _database_service.get_book_by_kosync_id(doc_hash)
        if linked_book:
            _database_service.link_kosync_document(doc_hash, linked_book.abs_id)

    # AUTO-DISCOVERY
    if not linked_book:
        auto_create = os.environ.get('AUTO_CREATE_EBOOK_MAPPING', 'true').lower() == 'true'

        if auto_create:
            if doc_hash not in _active_scans:
                _active_scans.add(doc_hash)

                def run_auto_discovery(doc_hash_val):
                    try:
                        from src.db.models import PendingSuggestion
                        import json
                        
                        logger.info(f"🔍 KOSync: Scheduled auto-discovery for unmapped document {doc_hash_val}")
                        epub_filename = _try_find_epub_by_hash(doc_hash_val)

                        if not epub_filename:
                            logger.debug(f"Could not auto-match EPUB for KOSync document '{doc_hash_val}'")
                            return
                        
                        title = Path(epub_filename).stem
                        
                        # Step 1: Check if there's a matching audiobook in ABS
                        audiobook_matches = []
                        if _container.abs_client().is_configured():
                            try:
                                audiobooks = _container.abs_client().get_all_audiobooks()
                                search_term = title
                                
                                logger.debug(f"Auto-discovery: Searching for audiobook matching '{search_term}' in {len(audiobooks)} audiobooks")
                                
                                for ab in audiobooks:
                                    media = ab.get('media', {})
                                    metadata = media.get('metadata', {})
                                    ab_title = (metadata.get('title') or ab.get('name', ''))
                                    ab_author = metadata.get('authorName', '')
                                    
                                    # Use same simple matching as UI search (normalized substring)
                                    def normalize(s):
                                        import re
                                        return re.sub(r'[^\w\s]', '', s.lower())
                                    
                                    search_norm = normalize(search_term)
                                    title_norm = normalize(ab_title)
                                    author_norm = normalize(ab_author)
                                    
                                    if (search_norm and title_norm) and (search_norm in title_norm or title_norm in search_norm):
                                        # Skip books with high progress (>75%) - they're already mostly done
                                        duration = media.get('duration', 0)
                                        progress_pct = 0
                                        if duration > 0:
                                            # Get progress from ABS for this audiobook
                                            try:
                                                ab_progress = _container.abs_client().get_progress(ab['id'])
                                                if ab_progress:
                                                    progress_pct = ab_progress.get('progress', 0) * 100
                                            except Exception as e:
                                                logger.debug(f"Failed to get ABS progress during auto-discovery: {e}")
                                        
                                        if progress_pct > 75:
                                            logger.debug(f"Auto-discovery: Skipping '{ab_title}' - already {progress_pct:.0f}% complete")
                                            continue
                                        
                                        logger.debug(f"Auto-discovery: Matched '{ab_title}' by {ab_author} for search term '{search_term}'")
                                        audiobook_matches.append({
                                            "source": "abs",
                                            "abs_id": ab['id'],
                                            "title": ab_title,
                                            "author": ab_author,
                                            "duration": duration,
                                            "confidence": "high"
                                        })
                                        
                            except Exception as e:
                                logger.warning(f"⚠️ Error searching ABS for audiobooks: {e}")
                        
                        # Step 2: If audiobook matches found, create a suggestion for user review
                        if audiobook_matches:
                            # Check if suggestion already exists (pending OR dismissed - don't re-suggest)
                            if not _database_service.suggestion_exists(doc_hash_val):
                                suggestion = PendingSuggestion(
                                    source_id=doc_hash_val,
                                    title=title,
                                    author=None,  # Could extract from EPUB metadata
                                    cover_url=f"/api/cover-proxy/{audiobook_matches[0]['abs_id']}",
                                    matches_json=json.dumps(audiobook_matches + [{
                                        "source": "ebook",
                                        "filename": epub_filename,
                                        "confidence": "high"
                                    }])
                                )
                                _database_service.save_pending_suggestion(suggestion)
                                logger.info(f"💡 Created suggestion for '{title}' - found {len(audiobook_matches)} audiobook match(es)")
                            return
                        
                        # Step 3: No audiobook found - fall back to ebook-only mapping
                        logger.info(f"📖 No audiobook match for '{title}' - creating ebook-only mapping")
                        book_id = f"ebook-{doc_hash_val[:16]}"
                        book = Book(
                            abs_id=book_id,
                            abs_title=title,
                            ebook_filename=epub_filename,
                            kosync_doc_id=doc_hash_val,
                            transcript_file=None,
                            status='active',
                            duration=None,
                            sync_mode='ebook_only'
                        )
                        _database_service.save_book(book)
                        _database_service.link_kosync_document(doc_hash_val, book_id)
                        _database_service.dismiss_suggestion(doc_hash_val)
                        logger.info(f"✅ Auto-created ebook-only mapping: {book_id} -> {epub_filename}")

                        if _manager:
                            _manager.sync_cycle(target_abs_id=book_id)
                            
                    except Exception as e:
                        logger.error(f"❌ Error in auto-discovery background task: {e}")
                    finally:
                        if doc_hash_val in _active_scans:
                            _active_scans.remove(doc_hash_val)

                threading.Thread(target=run_auto_discovery, args=(doc_hash,), daemon=True).start()

    if linked_book:
        # NOTE: We intentionally do NOT update book_states here.
        # The sync cycle is the only thing that should update book_states.
        # This ensures proper delta detection between cycles.
        logger.debug(f"KOSync: Updated linked book '{linked_book.abs_title}' to {percentage:.2%}")

        # Debounce sync trigger — wait until the reader stops turning pages
        # Skip if the update came from the sync bot itself (prevents sync→PUT→sync loop)
        # Skip if instant sync is globally disabled.
        if linked_book.status == 'active' and not is_internal:
            try:
                _update_grouped_kosync_session(
                    linked_book,
                    doc_hash,
                    device,
                    device_id,
                    percentage,
                    now_ts,
                )
            except Exception as e:
                logger.warning(f"KOSync session grouping failed for '{linked_book.abs_id}': {e}")

        instant_sync_enabled = os.environ.get('INSTANT_SYNC_ENABLED', 'true').lower() != 'false'
        if is_internal:
            # Internal writes (sync/reset flows) should cancel any pending user debounce
            # event for this book so we don't replay stale progress right after a reset.
            with _kosync_debounce_lock:
                if linked_book.abs_id in _kosync_debounce:
                    del _kosync_debounce[linked_book.abs_id]
                    logger.debug(f"KOSync PUT: Cleared pending debounce for internal update on '{linked_book.abs_title}'")
        if linked_book.status == 'active' and _manager and not is_internal and instant_sync_enabled:
            logger.debug(f"KOSync PUT: Progress event recorded for '{linked_book.abs_title}'")
            _record_kosync_event(linked_book.abs_id, linked_book.abs_title)

    response_timestamp = now.isoformat() + "Z"
    if device and device.lower() == "booknexus":
        # BookNexus expects an integer timestamp (Unix epoch)
        response_timestamp = int(now.timestamp())

    return jsonify({
        "document": doc_hash,
        "timestamp": response_timestamp
    }), 200


@kosync_sync_bp.route('/device-sync/manifest', methods=['GET'])
@kosync_sync_bp.route('/koreader/device-sync/manifest', methods=['GET'])
@kosync_auth_required
def koreader_device_sync_manifest():
    """Return the optional KOReader managed-folder sync manifest."""
    service = _get_koreader_device_sync_service()
    if not service:
        return jsonify({"error": "Device sync service unavailable"}), 503

    shelf_mapping = None
    collections_mode = os.environ.get("DEVICE_SYNC_COLLECTIONS", "off").lower()
    if collections_mode != "off":
        try:
            bl = _container.booklore_client()
            if bl.is_configured():
                excluded_raw = os.environ.get("DEVICE_SYNC_EXCLUDED_SHELVES", "")
                excludes = [s.strip() for s in excluded_raw.split(",") if s.strip()]
                # Auto-exclude the Grimmory sync shelf (e.g. "Kobo") — every
                # matched book is on it, so it would be a redundant collection.
                sync_shelf = os.environ.get("BOOKLORE_SHELF_NAME", "").strip()
                if sync_shelf and sync_shelf not in excludes:
                    excludes.append(sync_shelf)
                target_book_ids = []
                for book in service.database_service.get_books_by_status("active"):
                    source_id = getattr(book, "ebook_source_id", None)
                    if source_id:
                        target_book_ids.append(str(source_id))
                shelf_mapping = bl.get_book_shelf_mapping(
                    mode=collections_mode,
                    excludes=excludes,
                    target_book_ids=target_book_ids,
                )
        except Exception as e:
            logger.warning("Device-sync manifest: shelf mapping failed: %s", e)

    return jsonify(service.build_manifest(shelf_mapping=shelf_mapping)), 200


@kosync_sync_bp.route('/device-sync/books/<path:abs_id>/download', methods=['GET'])
@kosync_sync_bp.route('/koreader/device-sync/books/<path:abs_id>/download', methods=['GET'])
@kosync_auth_required
def koreader_device_sync_download(abs_id):
    """Download the original ebook for a bridge-managed KOReader sync item."""
    service = _get_koreader_device_sync_service()
    if not service:
        return jsonify({"error": "Device sync service unavailable"}), 503

    resolved = service.resolve_download(abs_id)
    if not resolved:
        return jsonify({"error": "Book not available"}), 404

    path = resolved["path"]
    filename = resolved["filename"]
    content_hash = resolved["content_hash"]
    mime_type = resolved.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    response = send_file(
        path,
        mimetype=mime_type,
        as_attachment=True,
        download_name=filename,
        conditional=True,
        etag=False,
        max_age=0,
    )
    response.set_etag(content_hash)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@kosync_sync_bp.route('/device-sync/sessions', methods=['POST'])
@kosync_sync_bp.route('/koreader/device-sync/sessions', methods=['POST'])
@kosync_auth_required
def kosync_upload_sessions():
    """Receive reading sessions from BridgeSync plugin and persist locally + forward to Grimmory."""
    data = request.json
    if not data or not isinstance(data, list):
        return jsonify({"error": "Expected JSON array"}), 400

    accepted = 0
    rejected = 0

    for session in data:
        abs_id = session.get('abs_id')
        doc_hash = session.get('document_hash')
        book = None
        kosync_doc = None

        if abs_id and _database_service:
            book = _database_service.get_book(abs_id)

        # Fallback: resolve via KOSync document hash
        if not book and _database_service:
            if doc_hash:
                book = _database_service.get_book_by_kosync_id(doc_hash)
                if book:
                    abs_id = book.abs_id

        if _database_service and doc_hash:
            kosync_doc = _database_service.get_kosync_document(doc_hash)

        if not book:
            logger.warning(f"Session upload: book not found for abs_id='{abs_id}' hash='{doc_hash}'")
            rejected += 1
            continue

        session_type = session.get('session_type', 'EBOOK')
        start_time = session.get('start_time', 0)
        end_time = session.get('end_time', 0)
        duration_seconds = session.get('duration_seconds', 0)
        start_progress = session.get('start_progress')
        end_progress = session.get('end_progress')

        # Plugin sends progress as 0-100, DB stores as 0-1
        if start_progress is not None:
            start_progress = float(start_progress) / 100.0
        if end_progress is not None:
            end_progress = float(end_progress) / 100.0

        try:
            _database_service.record_reading_session(
                abs_id=abs_id,
                session_type=session_type,
                start_time=float(start_time),
                end_time=float(end_time),
                duration_seconds=int(duration_seconds),
                start_progress=start_progress,
                end_progress=end_progress,
                leader_client="BridgeSync_Plugin",
            )
            accepted += 1
        except Exception as e:
            logger.warning(f"Session upload: failed to record session for '{abs_id}': {e}")
            rejected += 1
            continue

        if _database_service:
            try:
                deleted = _database_service.delete_recent_estimated_kosync_session(
                    abs_id=abs_id,
                    start_time=float(start_time),
                    end_time=float(end_time),
                    start_progress=start_progress,
                    end_progress=end_progress,
                )
                if deleted:
                    logger.info("Session upload: replaced overlapping estimated KoSync session for '%s'", abs_id)
            except Exception as e:
                logger.warning(f"Session upload: failed to dedupe estimated KoSync session for '{abs_id}': {e}")

        if kosync_doc and (kosync_doc.device_id or kosync_doc.device):
            seen_time = None
            try:
                seen_time = datetime.utcfromtimestamp(float(end_time)) if end_time is not None else None
            except (TypeError, ValueError, OSError):
                seen_time = None

            if _is_internal_kosync_device(kosync_doc.device, kosync_doc.device_id):
                logger.info(
                    "Session upload: skipping plugin classification for internal device '%s' (%s)",
                    kosync_doc.device or "unknown",
                    kosync_doc.device_id or "no-device-id",
                )
            else:
                try:
                    _upsert_kosync_device_session_entry(
                        device=kosync_doc.device,
                        device_id=kosync_doc.device_id,
                        mode="plugin",
                        source="plugin_session_auto",
                        last_document_hash=doc_hash,
                        seen_time=seen_time,
                    )
                    discarded = _discard_open_kosync_session(doc_hash, kosync_doc.device, kosync_doc.device_id)
                    logger.info(
                        "Session upload: classified device '%s' (%s) as plugin-backed%s",
                        kosync_doc.device or "unknown",
                        kosync_doc.device_id or "no-device-id",
                        " and dropped open estimated session" if discarded else "",
                    )
                except Exception as e:
                    logger.warning(f"Session upload: failed to classify plugin-backed device for '{abs_id}': {e}")

        # Fallback: discard any remaining open estimated sessions for this book by abs_id.
        # Handles the case where kosync_doc had no device info and the key-based discard above was
        # skipped entirely — e.g. when the plugin's document_hash maps to a stub KosyncDocument
        # with no device fields (common when the plugin file hash differs from the KOSync PUT hash).
        # Also classify those devices as plugin-backed so future PUT requests don't re-open sessions.
        fallback_discarded = _discard_open_kosync_sessions_for_book(abs_id)
        if fallback_discarded:
            logger.info(
                "Session upload: fallback-discarded %d open estimated session(s) for book '%s'",
                len(fallback_discarded),
                abs_id,
            )
            fallback_seen_time = None
            try:
                fallback_seen_time = datetime.utcfromtimestamp(float(end_time)) if end_time is not None else None
            except (TypeError, ValueError, OSError):
                pass
            for est_session in fallback_discarded:
                est_device = est_session.get("device")
                est_device_id = est_session.get("device_id")
                if (est_device or est_device_id) and not _is_internal_kosync_device(est_device, est_device_id):
                    try:
                        _upsert_kosync_device_session_entry(
                            device=est_device,
                            device_id=est_device_id,
                            mode="plugin",
                            source="plugin_session_auto",
                            last_document_hash=doc_hash,
                            seen_time=fallback_seen_time,
                        )
                        logger.info(
                            "Session upload: classified device '%s' (%s) as plugin-backed via open session fallback",
                            est_device or "unknown",
                            est_device_id or "no-device-id",
                        )
                    except Exception as e:
                        logger.warning(
                            "Session upload: failed to classify device from open session for '%s': %s",
                            abs_id, e,
                        )

        # Forward to Grimmory if configured
        if (
            os.environ.get("GRIMMORY_READING_SESSIONS", "true").lower() == "true"
            and _manager
            and hasattr(_manager, 'booklore_client')
            and _manager.booklore_client
            and _manager.booklore_client.is_configured()
        ):
            try:
                grimmory_id = _manager._resolve_grimmory_ebook_id(book)
                if grimmory_id:
                    _manager.booklore_client.create_reading_session(
                        book_id=int(grimmory_id),
                        start_time=float(start_time),
                        end_time=float(end_time),
                        start_progress=start_progress,
                        end_progress=end_progress,
                        book_type=session_type,
                    )
                    logger.debug(f"Forwarded session to Grimmory for '{book.abs_title}' (id={grimmory_id})")
            except Exception as e:
                logger.warning(f"Session upload: Grimmory forwarding failed for '{abs_id}': {e}")

    logger.info(f"Session upload: accepted={accepted}, rejected={rejected}")
    return jsonify({"accepted": accepted, "rejected": rejected}), 200


# ---------------- Helper Functions ----------------

def _upsert_kosync_metadata(document_hash, filename, source, mtime=None, booklore_id=None):
    """Cache hash metadata without overwriting any existing progress data."""
    from src.db.models import KosyncDocument

    existing = _database_service.get_kosync_document(document_hash)
    if existing:
        existing.filename = filename
        existing.source = source
        if mtime is not None:
            existing.mtime = mtime
        if booklore_id is not None:
            existing.booklore_id = booklore_id
        _database_service.save_kosync_document(existing)
    else:
        doc = KosyncDocument(
            document_hash=document_hash,
            filename=filename,
            source=source,
            mtime=mtime,
            booklore_id=booklore_id,
        )
        _database_service.save_kosync_document(doc)


def _try_find_epub_by_hash(doc_hash: str) -> Optional[str]:
    """Try to find matching EPUB file for a KOSync document hash."""
    try:
        # Check database for linked document first
        doc = _database_service.get_kosync_document(doc_hash)
        if doc and doc.filename:
            try:
                _container.ebook_parser().resolve_book_path(doc.filename)
                logger.info(f"📚 Matched EPUB via DB: {doc.filename}")
                return doc.filename
            except FileNotFoundError:
                logger.debug(f"🔍 DB suggested '{doc.filename}' but file is missing — Re-scanning")
        
        # [NEW] Check if valid linked book exists with original filename
        if doc and doc.linked_abs_id:
             book = _database_service.get_book(doc.linked_abs_id)
             if book and book.original_ebook_filename:
                 try:
                     _container.ebook_parser().resolve_book_path(book.original_ebook_filename)
                     logger.info(f"📚 Matched EPUB via Linked Book Original Filename: {book.original_ebook_filename}")
                     return book.original_ebook_filename
                 except Exception:
                     pass

        # Check filesystem
        if _ebook_dir and _ebook_dir.exists():
            logger.info(f"🔎 Starting filesystem search in {_ebook_dir} for hash {doc_hash}...")
            count = 0
            for epub_path in _ebook_dir.rglob("*.epub"):
                count += 1
                if count % 100 == 0:
                    logger.debug(f"Checked {count} local EPUBs...")

                # Optimization: Check if we already have this file's hash in DB
                cached_doc = _database_service.get_kosync_doc_by_filename(epub_path.name)
                if cached_doc:
                    # Check mtime for invalidation
                    current_mtime = epub_path.stat().st_mtime
                    if cached_doc.mtime == current_mtime:
                        if cached_doc.document_hash == doc_hash:
                            logger.info(f"📚 Matched EPUB via DB filename lookup: {epub_path.name}")
                            return epub_path.name
                        continue
                
                try:
                    computed_hash = _container.ebook_parser().get_kosync_id(epub_path)
                    
                    # Store/Update in DB
                    if cached_doc:
                        cached_doc.document_hash = computed_hash
                        cached_doc.mtime = epub_path.stat().st_mtime
                        cached_doc.source = 'filesystem'
                        _database_service.save_kosync_document(cached_doc)
                    else:
                        _upsert_kosync_metadata(computed_hash, epub_path.name, 'filesystem',
                                                mtime=epub_path.stat().st_mtime)

                    if computed_hash == doc_hash:
                        logger.info(f"📚 Matched EPUB via filesystem: {epub_path.name}")
                        return epub_path.name
                except Exception as e:
                    logger.debug(f"Error checking file {epub_path.name}: {e}")
            logger.info(f"🔍 Filesystem search finished. Checked {count} files. No match found")

        # Fallback to Grimmory
        if _container.booklore_client().is_configured():
            logger.info("🔎 Starting Grimmory API search...")

            try:
                # Query BookloreBook cache in DB first
                books = _database_service.get_all_booklore_books()
                scan_source = "Grimmory DB cache"
                if not books:
                    books = _container.booklore_client().get_all_books() or []
                    scan_source = "Grimmory in-memory cache"

                logger.info(f"Scanning {len(books)} books from {scan_source}...")

                for book in books:
                    if hasattr(book, 'raw_metadata_dict'):
                        raw_id = book.raw_metadata_dict.get('id')
                        book_id = str(raw_id) if raw_id is not None else None
                        filename = book.filename
                        book_title = getattr(book, 'title', None)

                        # Fallback to parsing raw_metadata if needed
                        if not book_id:
                            import json
                            try:
                                meta = json.loads(book.raw_metadata)
                                raw_id = meta.get('id')
                                book_id = str(raw_id) if raw_id is not None else None
                            except (json.JSONDecodeError, AttributeError) as e:
                                logger.debug(f"Failed to parse raw_metadata JSON: {e}")
                                continue
                    else:
                        raw_id = book.get('id')
                        book_id = str(raw_id) if raw_id is not None else None
                        filename = book.get('fileName')
                        book_title = book.get('title')

                        if book_id and not filename:
                            hydrated = _container.booklore_client()._fetch_and_cache_detail(raw_id)
                            if hydrated:
                                book = hydrated
                                filename = hydrated.get('fileName')
                                book_title = hydrated.get('title')

                    if not book_id or not filename:
                        logger.debug("Skipping Grimmory candidate without both id and filename")
                        continue
                    if not hasattr(book, 'raw_metadata_dict'):
                        book = SimpleNamespace(filename=filename, title=book_title)

                    # Check if we have a KosyncDocument for this Grimmory ID
                    cached_doc = _database_service.get_kosync_doc_by_booklore_id(book_id)
                    if cached_doc:
                        if cached_doc.document_hash == doc_hash:
                            logger.info(f"📚 Matched EPUB via Grimmory ID in DB: {book.filename}")
                            return filename

                    try:
                        book_content = _container.booklore_client().download_book(book_id)
                        if book_content:
                            computed_hash = _container.ebook_parser().get_kosync_id_from_bytes(filename, book_content)

                            if computed_hash == doc_hash:
                                safe_title = filename
                                cache_dir = _container.data_dir() / "epub_cache"
                                cache_dir.mkdir(parents=True, exist_ok=True)
                                cache_path = cache_dir / safe_title
                                with open(cache_path, 'wb') as f:
                                    f.write(book_content)
                                logger.info(f"📥 Persisted Grimmory book to cache: {safe_title}")

                                # Save/Update KosyncDocument in DB
                                if cached_doc:
                                    cached_doc.document_hash = computed_hash
                                    cached_doc.filename = safe_title
                                    cached_doc.source = 'booklore'
                                    _database_service.save_kosync_document(cached_doc)
                                else:
                                    _upsert_kosync_metadata(computed_hash, safe_title, 'booklore',
                                                            booklore_id=book_id)

                                logger.info(f"📚 Matched EPUB via Grimmory download: {safe_title}")
                                return safe_title
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to check Grimmory book '{book.title}': {e}")

                logger.info(f"🔍 Grimmory search finished. Checked {len(books)} books. No match found")

            except Exception as e:
                logger.debug(f"Error querying Grimmory for EPUB matching: {e}")

    except Exception as e:
        logger.error(f"❌ Error in EPUB auto-discovery: {e}")

    logger.info("🔍 Auto-discovery finished. No match found")
    return None


# ---------------- GET Fallback Helpers ----------------

def _respond_from_book_states(doc_id, book):
    """Build a GET response from a book's state data. Returns (response, status_code)."""
    states = _database_service.get_states_for_book(book.abs_id)

    # Also check sibling kosync_documents for device-specific progress
    sibling_docs = _database_service.get_kosync_documents_for_book(book.abs_id)
    docs_with_progress = [d for d in sibling_docs if d.percentage and float(d.percentage) > 0]
    if docs_with_progress:
        best_doc = max(docs_with_progress, key=lambda d: float(d.percentage))
        logger.info(f"KOSync: Resolved {doc_id} to '{book.abs_title}' via sibling hash {best_doc.document_hash} ({float(best_doc.percentage):.2%})")
        poison_pill = _suppress_empty_progress_response(doc_id, float(best_doc.percentage), best_doc.progress)
        if poison_pill is not None:
            return poison_pill
        return jsonify({
            "device": "abs-kosync-bridge",
            "device_id": "abs-kosync-bridge",
            "document": doc_id,
            "percentage": float(best_doc.percentage),
            "progress": best_doc.progress or "",
            "timestamp": int(best_doc.timestamp.timestamp()) if best_doc.timestamp else 0
        }), 200

    if not states:
        return jsonify({"message": "Document not found on server"}), 502

    kosync_state = next((s for s in states if s.client_name.lower() == 'kosync'), None)
    latest_state = kosync_state or max(states, key=lambda s: s.last_updated if s.last_updated else 0)
    latest_progress = (latest_state.xpath or latest_state.cfi or "") if hasattr(latest_state, 'xpath') else ""
    latest_pct = float(latest_state.percentage) if latest_state.percentage else 0
    poison_pill = _suppress_empty_progress_response(doc_id, latest_pct, latest_progress)
    if poison_pill is not None:
        return poison_pill

    return jsonify({
        "device": "abs-kosync-bridge",
        "device_id": "abs-kosync-bridge",
        "document": doc_id,
        "percentage": latest_pct,
        "progress": latest_progress,
        "timestamp": int(latest_state.last_updated) if latest_state.last_updated else 0
    }), 200


def _resolve_book_by_sibling_hash(doc_id: str, existing_doc=None):
    """
    Try to resolve an unknown hash to a known book using DB-only lookups.
    Checks if any other KosyncDocument with the same filename is already linked.
    """
    # Check if this hash has a filename cached (from a prior scan/PUT)
    doc = existing_doc or _database_service.get_kosync_document(doc_id)
    if doc and doc.filename:
        # Find a sibling document with the same filename that's linked to a book
        sibling = _database_service.get_kosync_doc_by_filename(doc.filename)
        if sibling and sibling.linked_abs_id and sibling.document_hash != doc_id:
            book = _database_service.get_book(sibling.linked_abs_id)
            if book:
                logger.info(f"🔗 KOSync: Resolved {doc_id} to '{book.abs_title}' via filename sibling")
                return book

        # Check if the filename matches a book's ebook_filename directly
        book = _database_service.get_book_by_ebook_filename(doc.filename)
        if book:
            logger.info(f"🔗 KOSync: Resolved {doc_id} to '{book.abs_title}' via ebook filename match")
            return book

    return None


def _register_hash_for_book(doc_id: str, book):
    """Register a new hash and link it to an existing book."""
    from src.db.models import KosyncDocument as KD

    existing = _database_service.get_kosync_document(doc_id)
    if existing:
        if not existing.linked_abs_id:
            _database_service.link_kosync_document(doc_id, book.abs_id)
            logger.info(f"🔗 KOSync: Linked existing document {doc_id} to '{book.abs_title}'")
    else:
        doc = KD(document_hash=doc_id, linked_abs_id=book.abs_id)
        _database_service.save_kosync_document(doc)
        logger.info(f"🔗 KOSync: Created and linked new document {doc_id} to '{book.abs_title}'")


def _run_get_auto_discovery(doc_id: str):
    """Background auto-discovery triggered by GET for an unknown hash.
    Finds the matching epub and links the hash to an existing book."""
    try:
        logger.info(f"🔍 KOSync: Background discovery (GET) for {doc_id}...")
        epub_filename = _try_find_epub_by_hash(doc_id)

        if not epub_filename:
            logger.info(f"🔍 KOSync: GET-discovery found no epub for {doc_id}...")
            return

        # Update stub with filename
        doc = _database_service.get_kosync_document(doc_id)
        if doc and not doc.filename:
            doc.filename = epub_filename
            _database_service.save_kosync_document(doc)

        # Try to find an existing book that uses this epub
        book = _database_service.get_book_by_ebook_filename(epub_filename)
        if book:
            _database_service.link_kosync_document(doc_id, book.abs_id)
            logger.info(f"✅ KOSync: GET-discovery linked {doc_id} to '{book.abs_title}'")
            return

        logger.info(f"🔍 KOSync: GET-discovery found epub '{epub_filename}' but no matching book")
    except Exception as e:
        logger.error(f"❌ Error in GET auto-discovery: {e}")
    finally:
        _active_scans.discard(doc_id)


# ---------------- KOSync Document Management API ----------------

def _suppress_empty_progress_response(doc_id: str, percentage: float, progress: Optional[str]):
    safe_progress = progress.strip() if isinstance(progress, str) else ""
    if percentage > 0 and not safe_progress:
        logger.warning(
            "KOSync: Suppressing response for %s - percentage %.2f%% but no locator available. Returning 502 to prevent page-0 reset.",
            doc_id,
            percentage * 100.0,
        )
        return jsonify({"message": "Document not found on server"}), 502
    return None

@kosync_admin_bp.route('/api/kosync-documents', methods=['GET'])
def api_get_kosync_documents():
    """Get all KOSync documents with their link status."""
    docs = _database_service.get_all_kosync_documents()
    result = []
    for doc in docs:
        linked_book = None
        if doc.linked_abs_id:
            linked_book = _database_service.get_book(doc.linked_abs_id)

        result.append({
            'document_hash': doc.document_hash,
            'progress': doc.progress,
            'percentage': float(doc.percentage) if doc.percentage else 0,
            'device': doc.device,
            'device_id': doc.device_id,
            'timestamp': doc.timestamp.isoformat() if doc.timestamp else None,
            'first_seen': doc.first_seen.isoformat() if doc.first_seen else None,
            'last_updated': doc.last_updated.isoformat() if doc.last_updated else None,
            'linked_abs_id': doc.linked_abs_id,
            'linked_book_title': linked_book.abs_title if linked_book else None
        })

    return jsonify({
        'documents': result,
        'total': len(result),
        'linked': sum(1 for d in result if d['linked_abs_id']),
        'unlinked': sum(1 for d in result if not d['linked_abs_id'])
    })


@kosync_admin_bp.route('/api/kosync-documents/<doc_hash>/link', methods=['POST'])
def api_link_kosync_document(doc_hash):
    """Link a KOSync document to an ABS book."""
    data = request.json
    if not data or 'abs_id' not in data:
        return jsonify({'error': 'Missing abs_id'}), 400

    abs_id = data['abs_id']

    book = _database_service.get_book(abs_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    doc = _database_service.get_kosync_document(doc_hash)
    if not doc:
        return jsonify({'error': 'KOSync document not found'}), 404

    success = _database_service.link_kosync_document(doc_hash, abs_id)
    if success:
        # [FIX] Always update the book's KOSync ID to match what we just linked.
        # This handles cases where the book had a "wrong" hash (e.g. from Storyteller artifact)
        # and we want to align it with the actual device hash.
        current_id = book.kosync_doc_id
        if current_id != doc_hash:
            logger.info(f"🔗 Updating Book {book.abs_title} KOSync ID: {current_id} -> {doc_hash}")
            book.kosync_doc_id = doc_hash
            _database_service.save_book(book)
        elif not current_id:
            book.kosync_doc_id = doc_hash
            _database_service.save_book(book)

        # Cleanup: Dismiss any pending suggestion for this document since it's now linked
        _database_service.dismiss_suggestion(doc_hash)

        return jsonify({'success': True, 'message': f'Linked to {book.abs_title}'})

    return jsonify({'error': 'Failed to link document'}), 500


@kosync_admin_bp.route('/api/kosync-documents/<doc_hash>/unlink', methods=['POST'])
def api_unlink_kosync_document(doc_hash):
    """Remove the ABS book link from a KOSync document."""
    success = _database_service.unlink_kosync_document(doc_hash)
    if success:
        # Cleanup cached EPUB for this hash
        _cleanup_cache_for_hash(doc_hash)
        return jsonify({'success': True, 'message': 'Document unlinked'})
    return jsonify({'error': 'Document not found'}), 404


@kosync_admin_bp.route('/api/kosync-documents/<doc_hash>', methods=['DELETE'])
def api_delete_kosync_document(doc_hash):
    """Delete a KOSync document."""
    success = _database_service.delete_kosync_document(doc_hash)
    if success:
        # Cleanup cached EPUB for this hash
        _cleanup_cache_for_hash(doc_hash)
        return jsonify({'success': True, 'message': 'Document deleted'})
    return jsonify({'error': 'Document not found'}), 404


def _cleanup_cache_for_hash(doc_hash):
    """Delete cached EPUB file for a document."""
    try:
        # Identify filename from DB
        doc = _database_service.get_kosync_document(doc_hash)
        filename = doc.filename if doc else None
        
        # Fallback: check linked book
        if not filename and doc and doc.linked_abs_id:
            book = _database_service.get_book(doc.linked_abs_id)
            if book:
                filename = book.original_ebook_filename or book.ebook_filename

        if filename:
            # Delete file if in epub_cache
            if _container:
                cache_dir = _container.data_dir() / "epub_cache"
                file_path = cache_dir / filename
                if file_path.exists():
                    try:
                        os.remove(file_path)
                        logger.info(f"🗑️ Deleted cached EPUB: {filename}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to delete cached file '{filename}': {e}")
        
        # Note: We don't delete the KosyncDocument record here, 
        # as it may contain important progress data. 
        # The filename/mtime/source fields just become stale or are cleared if unlinked.

    except Exception as e:
        logger.error(f"❌ Error cleaning up cache for '{doc_hash}': {e}")
