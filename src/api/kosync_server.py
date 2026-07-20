# KoSync Server - Extracted from web_server.py for clean code separation
# Implements KOSync protocol compatible with kosync-dotnet
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from flask import Blueprint, jsonify, request, send_file, g

from src.api.booklore_client import BookloreClient
from src.api.hardcover_client import HardcoverClient
from src.utils.cache_paths import safe_cache_path
from src.utils.kosync_headers import hash_kosync_key
from src.utils.time_utils import utcnow
from src.utils.user_context import set_current_user_id, reset_current_user_id
from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY, resolve_setting
from src.utils.string_utils import calculate_similarity, clean_book_title
from src.services.llm_matching import judge_best_candidate
from src.db.models import State

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
_active_scans_lock = threading.Lock()

# Bounded auto-discovery: cap concurrent + queued unique scans at 16.
_MAX_CONCURRENT_DISCOVERY = 2
_MAX_QUEUED_DISCOVERY = 16
_discovery_executor = ThreadPoolExecutor(
    max_workers=_MAX_CONCURRENT_DISCOVERY,
    thread_name_prefix="kosync-discovery",
)
_queued_discovery_count = 0
_queued_discovery_lock = threading.Lock()

_AUTH_FAILURES: dict[tuple[str, str], list[float]] = {}
_AUTH_FAILURES_LOCK = threading.Lock()
_AUTH_FAILURE_LIMIT = 5
_AUTH_FAILURE_WINDOW = 60

# Plugin self-update: resolved from src/api/ -> project root -> plugins/
_PLUGIN_DIR = Path(__file__).parent.parent.parent / "plugins" / "bridgesync.koplugin"
_plugin_zip_cache: Optional[tuple] = None  # (zip_bytes: bytes, max_mtime: float)
_plugin_zip_cache_lock = threading.Lock()

# Manifest pre-cache: background thread rebuilds this; endpoint reads from it.
_manifest_cache: Optional[dict] = None
_manifest_cache_lock = threading.Lock()
_manifest_rebuild_event = threading.Event()
_manifest_prebuilder_started = False
_booklore_shelf_mapping_cache: dict = {}
_booklore_shelf_mapping_cache_lock = threading.Lock()
_BOOKLORE_SHELF_MAPPING_TTL_SECONDS = 86400
_hardcover_list_mapping_cache: dict = {}
_hardcover_list_mapping_cache_lock = threading.Lock()
_HARDCOVER_LIST_MAPPING_TTL_SECONDS = 86400

# KoSync PUT debounce state
_kosync_debounce: dict = {}  # {(abs_id, user_id): {'last_event': float, 'title': str, 'synced': bool, 'user_id', 'abs_id'}}
_kosync_debounce_lock = threading.Lock()
_debounce_thread_started = False
_kosync_open_sessions: dict = {}  # {session_key: session_dict}
_kosync_open_sessions_lock = threading.Lock()
_KOSYNC_SESSION_GAP_SECONDS = 300
_KOSYNC_SESSION_MIN_SECONDS = 30
_KOSYNC_SESSION_MAX_SECONDS = 7200
_KOSYNC_PUT_DEBOUNCE_SECONDS_DEFAULT = 300
_KOSYNC_DEVICE_SESSION_REGISTRY_KEY = "KOSYNC_DEVICE_SESSION_REGISTRY"
_kosync_device_session_registry = None
_kosync_device_session_registry_lock = threading.Lock()
_kosync_recent_external_puts: dict = {}
_kosync_recent_external_puts_lock = threading.Lock()
_KOREADER_STATS_MAX_BOOKS = 1000
_KOREADER_STATS_MAX_PAGE_STATS = 10000
_KOREADER_STATS_MERGE_LIMIT = 10000
_BRIDGESYNC_LOG_MAX_LINES = 200
_BRIDGESYNC_LOG_MAX_LINE_CHARS = 1000
_BRIDGESYNC_LOG_MAX_PAYLOAD_BYTES = 64 * 1024


def _recent_external_put_ttl_seconds() -> int:
    configured = os.environ.get("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS")
    if configured:
        try:
            return max(0, int(configured))
        except ValueError:
            logger.warning("Invalid KOSYNC_RECENT_EXTERNAL_PUT_SECONDS=%r; using default", configured)
    return 600


def _record_recent_external_kosync_put(
    document_hash: str,
    device: str | None,
    device_id: str | None,
    percentage,
    now_ts: float,
    user_id=None,
) -> None:
    if not document_hash:
        return
    with _kosync_recent_external_puts_lock:
        _kosync_recent_external_puts[(document_hash, user_id)] = {
            "timestamp": now_ts,
            "device": device or "",
            "device_id": device_id or "",
            "percentage": float(percentage or 0),
        }


def _recent_external_kosync_put_metadata(document_hash: str | None, percentage=None, user_id=None) -> dict:
    if not document_hash:
        return {}
    now_ts = time.time()
    ttl = _recent_external_put_ttl_seconds()
    with _kosync_recent_external_puts_lock:
        entry = _kosync_recent_external_puts.get((document_hash, user_id))
        if not entry:
            return {}
        age = now_ts - float(entry.get("timestamp") or 0)
        if ttl <= 0 or age > ttl:
            _kosync_recent_external_puts.pop((document_hash, user_id), None)
            return {}

    if percentage is not None:
        try:
            if abs(float(entry.get("percentage") or 0) - float(percentage or 0)) > 0.0001:
                return {}
        except (TypeError, ValueError):
            return {}

    return {
        "_bridge_recent_external_put": True,
        "_bridge_recent_external_put_age_seconds": round(age, 3),
        "_bridge_recent_external_put_device": entry.get("device") or "",
        "_bridge_recent_external_put_device_id": entry.get("device_id") or "",
    }

def signal_manifest_rebuild() -> None:
    """Wake the manifest prebuilder thread so it rebuilds on the next cycle."""
    _manifest_rebuild_event.set()


def _compute_manifest_revision(items) -> str:
    """Deterministic revision over a manifest's book items.

    Mirrors KOReaderDeviceSyncService._compute_revision so a user-scoped manifest
    advertises a stable, content-derived revision the koplugin can diff — computed
    locally so serving never depends on rebuilding via the device-sync service."""
    digest_items = [
        {
            "abs_id": item.get("abs_id"),
            "filename": item.get("filename"),
            "content_hash": item.get("content_hash"),
            "size": item.get("size"),
            "shelves": item.get("shelves") or [],
        }
        for item in sorted(items, key=lambda value: str(value.get("abs_id")))
    ]
    payload = json.dumps(digest_items, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _device_collection_source(credentials: Optional[dict] = None) -> str:
    source = str(resolve_setting(credentials, "DEVICE_SYNC_COLLECTION_SOURCE", "off") or "off").strip().lower()
    if source not in {"off", "grimmory", "hardcover"}:
        source = "off"
    mode = str(resolve_setting(credentials, "DEVICE_SYNC_COLLECTIONS", "off") or "off").strip().lower()
    if source == "grimmory" and mode == "off":
        return "off"
    return source


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _manifest_fallback_allowed(user_id) -> bool:
    """Whether manifest collection resolution may fall back to the global config.

    Mirrors the app-wide policy (see _bind_request_user_context): admins inherit
    the shared os.environ settings/credentials, regular users are isolated to
    their own so one reader's shelves/lists never bleed into another's manifest."""
    try:
        user = _database_service.get_user(user_id) if _database_service else None
    except Exception as e:
        logger.warning("Manifest fallback-policy lookup failed (user_id=%s): %s", user_id, e)
        return False
    return bool(user and getattr(user, "is_admin", False))


def _booklore_credentials_for_manifest(user_id):
    if user_id is None or _database_service is None:
        return None
    try:
        credentials = _database_service.get_user_credentials(user_id)
    except Exception as e:
        logger.warning("Manifest Grimmory shelf credentials lookup failed (user_id=%s): %s", user_id, e)
        return {_ALLOW_GLOBAL_FALLBACK_KEY: False}
    credentials[_ALLOW_GLOBAL_FALLBACK_KEY] = _manifest_fallback_allowed(user_id)
    return credentials


def _booklore_shelf_cache_key(user_id, credentials: Optional[dict]) -> tuple:
    server = resolve_setting(credentials, "BOOKLORE_SERVER", "") or ""
    username = resolve_setting(credentials, "BOOKLORE_USER", "") or ""
    password = resolve_setting(credentials, "BOOKLORE_PASSWORD", "") or ""
    library_id = resolve_setting(credentials, "BOOKLORE_LIBRARY_ID", "") or ""
    secret_hash = hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()[:16]
    mode = str(resolve_setting(credentials, "DEVICE_SYNC_COLLECTIONS", "off") or "off").strip().lower()
    excludes = tuple(sorted(_split_csv(str(resolve_setting(credentials, "DEVICE_SYNC_EXCLUDED_SHELVES", "") or ""))))
    sync_shelf = resolve_setting(credentials, "BOOKLORE_SHELF_NAME", "") or ""
    global_sync_shelf = os.environ.get("BOOKLORE_SHELF_NAME", "") or ""
    return (user_id, server, library_id, secret_hash, mode, excludes, sync_shelf, global_sync_shelf)


def _is_booklore_manifest_book(book) -> bool:
    source = str(getattr(book, "ebook_source", "") or "").strip().lower()
    return source in {"booklore", "grimmory"} and bool(getattr(book, "ebook_source_id", None))


def _build_booklore_shelf_mapping(user_id) -> Optional[dict[str, list[str]]]:
    """Return Grimmory book id -> shelf names for the manifest user, cached daily."""
    credentials = _booklore_credentials_for_manifest(user_id)
    mode = str(resolve_setting(credentials, "DEVICE_SYNC_COLLECTIONS", "off") or "off").strip().lower()
    if mode == "off" or _database_service is None:
        return None

    cache_key = _booklore_shelf_cache_key(user_id, credentials)
    now = time.time()
    with _booklore_shelf_mapping_cache_lock:
        cached = _booklore_shelf_mapping_cache.get(cache_key)
        if cached and (now - cached["time"]) < _BOOKLORE_SHELF_MAPPING_TTL_SECONDS:
            return cached["mapping"]

    try:
        client = BookloreClient(database_service=_database_service, credentials=credentials)
        if not client.is_configured():
            return None
        excludes = _split_csv(str(resolve_setting(credentials, "DEVICE_SYNC_EXCLUDED_SHELVES", "") or ""))
        sync_shelves = [
            str(resolve_setting(credentials, "BOOKLORE_SHELF_NAME", "") or "").strip(),
            str(os.environ.get("BOOKLORE_SHELF_NAME", "") or "").strip(),
        ]
        for sync_shelf in sync_shelves:
            if sync_shelf and sync_shelf not in excludes:
                excludes.append(sync_shelf)
        books = _database_service.get_books_by_status("active", user_id=user_id) if user_id else _database_service.get_books_by_status("active")
        target_book_ids = [
            str(book.ebook_source_id)
            for book in books
            if _is_booklore_manifest_book(book)
        ]
        mapping = client.get_book_shelf_mapping(
            mode=mode,
            excludes=excludes,
            target_book_ids=target_book_ids,
        )
        with _booklore_shelf_mapping_cache_lock:
            _booklore_shelf_mapping_cache[cache_key] = {"time": now, "mapping": mapping}
        return mapping
    except Exception as e:
        logger.warning("Manifest Grimmory shelf mapping failed (user_id=%s): %s", user_id, e)
        with _booklore_shelf_mapping_cache_lock:
            cached = _booklore_shelf_mapping_cache.get(cache_key)
        return cached["mapping"] if cached else None


def _hardcover_list_cache_key(user_id, credentials: Optional[dict]) -> tuple:
    token = ""
    if credentials:
        token = str(credentials.get("HARDCOVER_TOKEN") or "")
    elif user_id is None:
        token = os.environ.get("HARDCOVER_TOKEN", "")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else ""
    mode = str(resolve_setting(credentials, "DEVICE_SYNC_HARDCOVER_LISTS", "all") or "all").strip().lower()
    names = tuple(sorted(_split_csv(str(resolve_setting(credentials, "DEVICE_SYNC_HARDCOVER_LIST_NAMES", "") or ""))))
    return (user_id, token_hash, mode, names)


def _hardcover_credentials_for_manifest(user_id):
    if user_id is None or _database_service is None:
        return None
    try:
        credentials = _database_service.get_user_credentials(user_id)
    except Exception as e:
        logger.warning("Manifest Hardcover list credentials lookup failed (user_id=%s): %s", user_id, e)
        return {_ALLOW_GLOBAL_FALLBACK_KEY: False}
    credentials[_ALLOW_GLOBAL_FALLBACK_KEY] = _manifest_fallback_allowed(user_id)
    return credentials


def _build_hardcover_list_mapping(user_id) -> Optional[dict[str, list[str]]]:
    """Return Hardcover book id -> list names for the manifest user, cached daily."""
    credentials = _hardcover_credentials_for_manifest(user_id)
    cache_key = _hardcover_list_cache_key(user_id, credentials)
    now = time.time()
    with _hardcover_list_mapping_cache_lock:
        cached = _hardcover_list_mapping_cache.get(cache_key)
        if cached and (now - cached["time"]) < _HARDCOVER_LIST_MAPPING_TTL_SECONDS:
            return cached["mapping"]

    try:
        client = HardcoverClient(credentials=credentials)
        if not client.is_configured():
            return None
        lists = client.get_user_lists()
        mode = str(resolve_setting(credentials, "DEVICE_SYNC_HARDCOVER_LISTS", "all") or "all").strip().lower()
        if mode == "selected":
            selected = {
                name.lower()
                for name in _split_csv(str(resolve_setting(credentials, "DEVICE_SYNC_HARDCOVER_LIST_NAMES", "") or ""))
            }
            lists = [entry for entry in lists if str(entry.get("name") or "").strip().lower() in selected]
        if not lists:
            mapping = {}
        else:
            list_names = {
                int(entry["id"]): str(entry.get("name") or "").strip()
                for entry in lists
                if entry.get("id") and str(entry.get("name") or "").strip()
            }
            memberships = client.get_list_book_memberships(list(list_names.keys()))
            mapping = {}
            for row in memberships:
                book_id = str(row.get("book_id") or "").strip()
                try:
                    list_id = int(row.get("list_id"))
                except (TypeError, ValueError):
                    continue
                if not book_id or list_id not in list_names:
                    continue
                mapping.setdefault(book_id, [])
                list_name = list_names[list_id]
                if list_name not in mapping[book_id]:
                    mapping[book_id].append(list_name)
        with _hardcover_list_mapping_cache_lock:
            _hardcover_list_mapping_cache[cache_key] = {"time": now, "mapping": mapping}
        return mapping
    except Exception as e:
        logger.warning("Manifest Hardcover list mapping failed (user_id=%s): %s", user_id, e)
        with _hardcover_list_mapping_cache_lock:
            cached = _hardcover_list_mapping_cache.get(cache_key)
        return cached["mapping"] if cached else None


def _apply_hardcover_list_collections(manifest: dict, user_id) -> None:
    mapping = _build_hardcover_list_mapping(user_id)
    if mapping is None or _database_service is None:
        return
    try:
        details_by_abs = {
            str(getattr(details, "abs_id", "") or ""): details
            for details in _database_service.get_all_hardcover_details()
        }
    except Exception as e:
        logger.warning("Manifest Hardcover details lookup failed (user_id=%s): %s", user_id, e)
        return
    for item in manifest.get("books") or []:
        abs_id = str(item.get("abs_id") or "")
        if not abs_id:
            continue
        details = details_by_abs.get(abs_id)
        hardcover_book_id = str(getattr(details, "hardcover_book_id", "") or "").strip()
        if not hardcover_book_id:
            item.pop("shelves", None)
            continue
        item["shelves"] = mapping.get(hardcover_book_id) or ["Unsorted"]


def _apply_booklore_shelf_collections(manifest: dict, user_id) -> None:
    mapping = _build_booklore_shelf_mapping(user_id)
    if mapping is None or _database_service is None:
        return
    try:
        books = _database_service.get_books_by_status("active", user_id=user_id) if user_id else _database_service.get_books_by_status("active")
    except Exception as e:
        logger.warning("Manifest Grimmory shelf book lookup failed (user_id=%s): %s", user_id, e)
        return
    books_by_abs = {str(book.abs_id): book for book in books}
    for item in manifest.get("books") or []:
        book = books_by_abs.get(str(item.get("abs_id") or ""))
        if not book or not _is_booklore_manifest_book(book):
            item.pop("shelves", None)
            continue
        source_id = str(getattr(book, "ebook_source_id", "") or "").strip()
        item["shelves"] = mapping.get(source_id) or ["Unsorted"]


def _apply_user_collection_source(manifest: dict, user_id) -> None:
    credentials = _booklore_credentials_for_manifest(user_id)
    source = _device_collection_source(credentials)
    if source == "grimmory":
        _apply_booklore_shelf_collections(manifest, user_id)
    elif source == "hardcover":
        _apply_hardcover_list_collections(manifest, user_id)


def _scope_manifest_to_user(manifest, user_id):
    """Return a copy of the manifest containing only the books owned by `user_id`.

    The prebuilt manifest cache covers every active book (one global build); each
    device must only receive its own user's matches, so we filter at serve time
    by ownership and recompute the revision over the trimmed set. `user_id` None
    (single-user install / no accounts) serves the manifest unscoped; when the
    user owns the whole manifest the original (revision included) is returned."""
    if not manifest or _database_service is None:
        return manifest
    if user_id is None:
        scoped = dict(manifest)
        scoped["books"] = [dict(item) for item in manifest.get("books") or []]
        _apply_user_collection_source(scoped, user_id)
        scoped["revision"] = _compute_manifest_revision(scoped["books"])
        return scoped
    try:
        owned_ids = {
            str(book.abs_id)
            for book in _database_service.get_books_by_status("active", user_id=user_id)
        }
    except Exception as e:
        logger.warning("Manifest user-scoping failed (user_id=%s): %s", user_id, e)
        return manifest
    all_books = manifest.get("books") or []
    books = [dict(item) for item in all_books if str(item.get("abs_id")) in owned_ids]
    credentials = _booklore_credentials_for_manifest(user_id)
    if len(books) == len(all_books) and _device_collection_source(credentials) == "off":
        return manifest
    scoped = dict(manifest)
    scoped["books"] = books
    _apply_user_collection_source(scoped, user_id)
    scoped["revision"] = _compute_manifest_revision(scoped["books"])
    return scoped


def _manifest_prebuilder_loop() -> None:
    """Daemon thread: rebuild manifest cache when signaled or every 60 seconds."""
    global _manifest_cache
    _REBUILD_INTERVAL = 60
    logger.info("Manifest prebuilder thread started")
    while True:
        _manifest_rebuild_event.wait(timeout=_REBUILD_INTERVAL)
        _manifest_rebuild_event.clear()
        service = _get_koreader_device_sync_service()
        if not service:
            continue
        try:
            manifest = service.build_manifest()
            with _manifest_cache_lock:
                _manifest_cache = manifest
            logger.debug(
                "Manifest cache rebuilt (%d books, revision=%.8s)",
                len(manifest.get("books", [])),
                manifest.get("revision", ""),
            )
        except Exception as e:
            logger.error("Manifest prebuilder error: %s", e)


def _start_manifest_prebuilder() -> None:
    global _manifest_prebuilder_started
    if not _manifest_prebuilder_started:
        _manifest_prebuilder_started = True
        threading.Thread(target=_manifest_prebuilder_loop, daemon=True).start()


def init_kosync_server(database_service, container, manager, ebook_dir=None):
    """Initialize KoSync server with required dependencies."""
    global _database_service, _container, _manager, _ebook_dir, _kosync_device_session_registry
    _database_service = database_service
    _container = container
    _manager = manager
    _ebook_dir = ebook_dir
    _kosync_device_session_registry = None
    _start_manifest_prebuilder()


def _get_koreader_device_sync_service():
    if not _container:
        return None
    try:
        return _container.koreader_device_sync_service()
    except Exception as e:
        logger.warning(f"KOReader device-sync service unavailable: {e}")
        return None


def _spawn_user_scoped_thread(target, args=(), user_id=None, name=None) -> None:
    """Spawn a daemon thread that runs ``target`` with the KoSync device's user
    bound as the ambient user.

    contextvars are NOT inherited by newly-created threads, so a raw
    ``threading.Thread`` running auto-discovery resolves ``get_current_user_id()``
    to ``None`` on the worker and mis-attributes any ``save_book`` /
    ``link_kosync_document`` to the default admin. Rebinding the id inside the
    worker keeps the auto-created mapping owned by the reader who triggered it.
    """
    def runner():
        token = set_current_user_id(user_id)
        try:
            target(*args)
        finally:
            reset_current_user_id(token)

    threading.Thread(target=runner, daemon=True, name=name).start()


def _record_kosync_event(abs_id: str, title: str, user_id=None) -> None:
    """Record a KoSync PUT event for debounced sync triggering.

    `user_id` is the BookBridge user the device authenticated as; the debounced
    sync runs for that user only (a device PUT is one person's progress)."""
    global _debounce_thread_started
    with _kosync_debounce_lock:
        # Key by (abs_id, user_id): two users reading the same shared book must
        # each get their own debounced sync — a bare abs_id key lets the second
        # PUT overwrite the first, dropping that user's cross-service propagation.
        _kosync_debounce[(abs_id, user_id)] = {
            'last_event': time.time(),
            'title': title,
            'synced': False,
            'user_id': user_id,
            'abs_id': abs_id,
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

    now_iso = (seen_time or utcnow()).isoformat() + "Z"
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


def _get_kosync_put_debounce_seconds() -> int:
    """Return the KoSync PUT debounce interval in seconds."""
    try:
        value = int(os.environ.get("KOSYNC_PUT_DEBOUNCE_SECONDS", str(_KOSYNC_PUT_DEBOUNCE_SECONDS_DEFAULT)))
        return max(0, value)
    except ValueError:
        return _KOSYNC_PUT_DEBOUNCE_SECONDS_DEFAULT


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


def _forward_reading_session_to_bookorbit(
    book, start_time, end_time, start_progress, end_progress, book_type=None
) -> None:
    """Forward a reading session to BookOrbit when the book's ebook is hosted there."""
    if os.environ.get("BOOKORBIT_READING_SESSIONS", "true").strip().lower() not in ("true", "1", "yes", "on"):
        return
    if not book or getattr(book, "ebook_source", None) != "BookOrbit":
        return
    client = getattr(_manager, "bookorbit_client", None) if _manager else None
    if not client or not client.is_configured():
        return
    source_id = getattr(book, "ebook_source_id", None)
    if not source_id:
        return
    try:
        client.create_reading_session(
            book_id=int(source_id),
            start_time=float(start_time),
            end_time=float(end_time),
            start_progress=start_progress,
            end_progress=end_progress,
            book_type=book_type,
        )
        logger.debug(f"Forwarded session to BookOrbit for '{getattr(book, 'abs_title', '?')}' (id={source_id})")
    except Exception as e:
        logger.warning(f"Session upload: BookOrbit forwarding failed for '{getattr(book, 'abs_id', '?')}': {e}")


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

    try:
        bo_book = _database_service.get_book(session_data["abs_id"])
        _forward_reading_session_to_bookorbit(
            bo_book,
            session_data["start_time"],
            session_data["last_time"],
            session_data["start_progress"],
            session_data["last_progress"],
            book_type=session_data["session_type"],
        )
    except Exception as e:
        logger.warning("KOSync BookOrbit session forwarding failed for '%s': %s", session_data["abs_id"], e)


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
    debounce_seconds = _get_kosync_put_debounce_seconds()
    while True:
        time.sleep(10)
        now = time.time()
        _flush_stale_kosync_sessions(now)
        to_sync = []

        with _kosync_debounce_lock:
            for _key, info in _kosync_debounce.items():
                if not info['synced'] and (now - info['last_event']) > debounce_seconds:
                    info['synced'] = True
                    to_sync.append((info['abs_id'], info['title'], info.get('user_id')))

        for abs_id, title, user_id in to_sync:
            if _manager:
                logger.info(f"⚡ KOSync PUT: Triggering sync for '{title}' (debounced)")
                # A device PUT is one user's progress: sync that user only when
                # known; otherwise (single-user/global) sync all eligible users.
                if user_id is not None:
                    target, kwargs = _manager.sync_cycle, {'target_abs_id': abs_id, 'user_id': user_id}
                else:
                    target, kwargs = _manager.run_sync_for_all_users, {'target_abs_id': abs_id}
                threading.Thread(target=target, kwargs=kwargs, daemon=True).start()

        # Clean up entries older than the same debounce window.
        with _kosync_debounce_lock:
            stale = [k for k, v in _kosync_debounce.items() if now - v['last_event'] > debounce_seconds]
            for k in stale:
                del _kosync_debounce[k]


def _kosync_creds_match(presented_key: str, stored_secret: str) -> bool:
    """KOReader sends either the raw key or its md5 hash."""
    if not presented_key or not stored_secret:
        return False
    return presented_key == stored_secret or presented_key == hash_kosync_key(stored_secret)


def _auth_failure_key(username: str) -> tuple[str, str]:
    return ((username or "").strip().lower(), request.remote_addr or "")


def _auth_is_limited(key: tuple[str, str], failed: bool = False) -> bool:
    now = time.time()
    with _AUTH_FAILURES_LOCK:
        recent = [t for t in _AUTH_FAILURES.get(key, []) if now - t < _AUTH_FAILURE_WINDOW]
        if failed:
            recent.append(now)
        if recent:
            _AUTH_FAILURES[key] = recent
        else:
            _AUTH_FAILURES.pop(key, None)
        return len(recent) >= _AUTH_FAILURE_LIMIT


def _kosync_user_may_access_book(book) -> bool:
    user_id = getattr(g, "kosync_user_id", None)
    if user_id is None or book is None:
        return True
    return bool(_database_service.is_user_linked(user_id, book.abs_id))


def authenticate_kosync(username: str, key: str):
    """Authenticate a KOReader (username, key).

    Returns (authenticated, user_id). Multi-user: each user stores their own
    KOSYNC_USER/KOSYNC_KEY, so different e-readers authenticate as different
    users. Falls back to the global KOSYNC_USER/KEY (single-user setup), in
    which case user_id is the default/admin user (or None if no users exist —
    still authenticated, scoping then falls back to the default).
    """
    if not username or not key:
        return False, None

    # 1. Per-user credentials
    if _database_service is not None and hasattr(_database_service, "list_users"):
        try:
            for u in _database_service.list_users():
                if not getattr(u, "active", 1):
                    continue
                creds = _database_service.get_user_credentials(u.id)
                ku, kk = creds.get("KOSYNC_USER"), creds.get("KOSYNC_KEY")
                if ku and username.lower() == ku.lower() and _kosync_creds_match(key, kk):
                    return True, u.id
        except Exception as e:
            logger.debug(f"KoSync user resolve (per-user) failed: {e}")

    # 2. Global fallback -> default (admin) user
    g_user = os.environ.get("KOSYNC_USER")
    g_pw = os.environ.get("KOSYNC_KEY")
    if g_user and username.lower() == g_user.lower() and _kosync_creds_match(key, g_pw):
        default_uid = None
        try:
            default_uid = _database_service._default_user_id() if _database_service else None
        except Exception:
            default_uid = None
        return True, default_uid
    return False, None


def kosync_auth_required(f):
    """Decorator for KOSync authentication. Resolves the device to a BookBridge
    user and scopes the request's state reads/writes to that user."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = request.headers.get('x-auth-user')
        key = request.headers.get('x-auth-key')
        failure_key = _auth_failure_key(user)
        if _auth_is_limited(failure_key):
            return jsonify({"error": "Too many authentication attempts"}), 429

        authenticated, user_id = authenticate_kosync(user, key)
        if not authenticated:
            _auth_is_limited(failure_key, failed=True)
            logger.warning(f"⚠️ KOSync Integrated Server: Unauthorized access attempt from '{request.remote_addr}' (user: '{user}')")
            return jsonify({"error": "Unauthorized"}), 401

        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES.pop(failure_key, None)
        g.kosync_user_id = user_id
        token = set_current_user_id(user_id)
        try:
            return f(*args, **kwargs)
        finally:
            reset_current_user_id(token)
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

    if not user or not key:
        logger.warning(f"⚠️ KOSync Auth: Missing credentials from '{request.remote_addr}'")
        return jsonify({"message": "Invalid credentials"}), 401

    authenticated, _ = authenticate_kosync(user, key)
    if authenticated:
        logger.debug(f"KOSync Auth: User '{user}' authenticated successfully")
        return jsonify({"username": user}), 200

    logger.warning(f"⚠️ KOSync Auth: Failed auth attempt for user '{user}' from '{request.remote_addr}'")
    return jsonify({"message": "Unauthorized"}), 401


@kosync_sync_bp.route('/users/create', methods=['POST'])
@kosync_sync_bp.route('/koreader/users/create', methods=['POST'])
def kosync_users_create():
    """Stub for KOReader user registration.

    BookBridge manages accounts in its web UI, not via KOReader registration, so
    this only acknowledges the request. It echoes back the *requested* username
    (never the server's configured KOSYNC_USER) to avoid disclosing credentials
    to an unauthenticated caller.
    """
    data = request.get_json(silent=True) or {}
    requested = data.get("username") or request.form.get("username") or ""
    return jsonify({"username": requested}), 201


@kosync_sync_bp.route('/users/login', methods=['POST'])
@kosync_sync_bp.route('/koreader/users/login', methods=['POST'])
def kosync_users_login():
    """KOReader login check.

    Validates the supplied credentials (header or JSON body) and returns success
    without disclosing any secret. Previously this stub echoed back KOSYNC_KEY as
    a "token" to any unauthenticated caller, leaking the global sync key.
    """
    user = request.headers.get('x-auth-user')
    key = request.headers.get('x-auth-key')
    if not user or not key:
        data = request.get_json(silent=True) or {}
        user = user or data.get("username")
        key = key or data.get("password")

    authenticated, _ = authenticate_kosync(user, key)
    if not authenticated:
        logger.warning(f"⚠️ KOSync Login: Failed attempt for user '{user}' from '{request.remote_addr}'")
        return jsonify({"message": "Unauthorized"}), 401

    return jsonify({"username": user}), 200


@kosync_sync_bp.route('/syncs/progress/<doc_id>', methods=['GET'])
@kosync_sync_bp.route('/koreader/syncs/progress/<doc_id>', methods=['GET'])
@kosync_auth_required
def kosync_get_progress(doc_id):
    """
    Fetch progress for a specific document.
    Returns 404 if document not found. (kosync-dotnet returns 502 here, the original
    sync server returns 200 + {}; we use 404 because strict clients like Crosspoint
    treat 5xx as a fatal server error and abort sync, while KOReader treats any
    non-200 the same. See issue #332.)

    Lookup order:
      1. Direct hash match in kosync_documents
      2. Book lookup by kosync_doc_id
      3. Sibling hash resolution (same book, different epub hash)
      4. Background auto-discovery for completely unknown hashes
    """
    if not doc_id or doc_id.lower() in ("none", "null"):
        logger.warning(
            f"⚠️ KOSync: Invalid or placeholder document ID requested: '{doc_id}'. This cannot be resolved."
        )
        return jsonify({"message": "Document not found on server"}), 404

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
                if not _kosync_user_may_access_book(book):
                    return jsonify({"message": "Document not found on server"}), 404
                return _respond_from_book_states(doc_id, book)

        resolved_book = _resolve_book_by_sibling_hash(doc_id, existing_doc=kosync_doc)
        if resolved_book:
            if not _kosync_user_may_access_book(resolved_book):
                return jsonify({"message": "Document not found on server"}), 404
            _register_hash_for_book(doc_id, resolved_book)
            return _respond_from_book_states(doc_id, resolved_book)

        request_user_id = getattr(g, "kosync_user_id", None)
        # Per-user device progress lives in kosync_user_progress. Fall back to the
        # shared kosync_doc row (legacy / pre-migration) only when it is this
        # user's own or unstamped (legacy) — never another user's position.
        progress_row = _database_service.get_user_kosync_progress(doc_id, request_user_id)
        if progress_row is None:
            doc_user_id = getattr(kosync_doc, "user_id", None)
            if request_user_id is None or doc_user_id in (None, request_user_id):
                progress_row = kosync_doc
        has_progress = (
            progress_row is not None
            and progress_row.percentage
            and float(progress_row.percentage) > 0
        )
        if has_progress:
            poison_pill = _suppress_empty_progress_response(
                doc_id,
                float(progress_row.percentage),
                progress_row.progress,
            )
            if poison_pill is not None:
                return poison_pill
            response_data = {
                "device": progress_row.device or "",
                "device_id": progress_row.device_id or "",
                "document": doc_id,
                "percentage": float(progress_row.percentage),
                "progress": progress_row.progress or "",
                "timestamp": int(progress_row.timestamp.timestamp()) if progress_row.timestamp else 0
            }
            response_data.update(
                _recent_external_kosync_put_metadata(
                    doc_id,
                    response_data["percentage"],
                    request_user_id,
                )
            )
            return jsonify(response_data), 200
        # Document exists but has no progress and no linked book — fall through
        # to try sibling resolution for better data

    # Step 2: Book lookup by kosync_doc_id
    book = _database_service.get_book_by_kosync_id(doc_id)
    if book:
        if not _kosync_user_may_access_book(book):
            return jsonify({"message": "Document not found on server"}), 404
        return _respond_from_book_states(doc_id, book)

    # Step 3: Sibling hash resolution — find the book via other linked hashes.
    # Skip when a kosync_doc existed: Step 1 already ran this identical resolution
    # (same doc_id/existing_doc) above with no intervening state change, so a
    # second call is deterministically redundant.
    if kosync_doc is None:
        resolved_book = _resolve_book_by_sibling_hash(doc_id, existing_doc=kosync_doc)
        if resolved_book:
            if not _kosync_user_may_access_book(resolved_book):
                return jsonify({"message": "Document not found on server"}), 404
            _register_hash_for_book(doc_id, resolved_book)
            return _respond_from_book_states(doc_id, resolved_book)

    # Step 4: Unknown hash — register stub and start background discovery
    if not _is_valid_hex_hash(doc_id):
        logger.warning(
            f"⚠️ KOSync: Rejected malformed document ID: '{doc_id}' (GET from {request.remote_addr})"
        )
        return jsonify({"message": "Document not found on server"}), 404
    _schedule_auto_discovery(
        doc_id,
        user_id=getattr(g, "kosync_user_id", None),
        source="get",
    )

    logger.warning(
        f"⚠️ KOSync: Document not found: {doc_id} (GET from {request.remote_addr}). "
        "If auto-discovery can't match it (e.g. the device's copy isn't byte-identical to "
        "the library file), link it manually from Add / Update Book -> Reader Documents, or re-deliver "
        "the book via the BridgeSync plugin's 'Sync books' so the hash matches."
    )
    return jsonify({"message": "Document not found on server"}), 404


def _autodiscovery_audiobook_candidates(epub_filename, ebook_meta):
    """Score every ABS audiobook against the EPUB's title/author.

    Returns candidates (loosely gated on title overlap) with real similarity scores,
    identifiers, and listening progress — the input for both auto-map and suggestions.
    """
    candidates = []
    try:
        abs_client = _container.abs_client()
    except Exception:
        return candidates
    if not abs_client.is_configured():
        return candidates

    title = (ebook_meta.get("title") or "").strip() or Path(epub_filename).stem
    author = (ebook_meta.get("author") or "").strip()
    clean_title = clean_book_title(title)

    try:
        audiobooks = abs_client.get_all_audiobooks()
    except Exception as e:
        logger.warning(f"⚠️ Auto-discovery: error listing audiobooks: {e}")
        return candidates

    logger.debug(f"Auto-discovery: scoring '{title}' (author='{author}') against {len(audiobooks)} audiobooks")
    for ab in audiobooks:
        media = ab.get("media", {}) or {}
        metadata = media.get("metadata", {}) or {}
        ab_title = metadata.get("title") or ab.get("name", "") or ""
        ab_author = metadata.get("authorName", "") or ""
        if not ab_title:
            continue

        title_sim = calculate_similarity(clean_title, clean_book_title(ab_title))
        if title_sim < 0.55:
            continue
        author_sim = calculate_similarity(author.lower(), ab_author.lower()) if (author and ab_author) else None

        duration = media.get("duration", 0)
        progress_pct = 0
        if duration and duration > 0:
            try:
                ab_progress = abs_client.get_progress(ab["id"])
                if ab_progress:
                    progress_pct = ab_progress.get("progress", 0) * 100
            except Exception as e:
                logger.debug(f"Auto-discovery: failed to get ABS progress: {e}")

        candidates.append({
            "abs_id": ab["id"],
            "title": ab_title,
            "author": ab_author,
            "isbn": (metadata.get("isbn") or "").strip(),
            "asin": (metadata.get("asin") or "").strip(),
            "duration": duration,
            "progress_pct": progress_pct,
            "title_sim": title_sim,
            "author_sim": author_sim,
        })

    candidates.sort(key=lambda c: c["title_sim"], reverse=True)
    return candidates


def _trailing_volume(title):
    """Extract a trailing volume number, ignoring trailing parentheticals like '(Unabridged)'."""
    text = (title or "").strip()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    match = re.search(r"(\d+)\s*$", text)
    return match.group(1) if match else None


def _select_auto_map_candidate(ebook_meta, candidates):
    """Decide whether one audiobook is an exact, unambiguous match worth auto-mapping.

    Tier 0: EPUB ISBN/ASIN matches an audiobook identifier (authoritative, no LLM).
    Tier 1: among the strong fuzzy candidates (title>=0.90, author>=0.80, capped at 3),
    the Ollama judge confidently picks the same work, no confusingly-named rival sits just
    outside the strong set, and the chosen volume number matches the EPUB's. Returns
    (candidate, reason) or (None, None).
    """
    pool = [c for c in candidates if (c.get("progress_pct") or 0) <= 75]
    if not pool:
        return None, None

    # Tier 0 — identifier match.
    epub_ids = {str(v).replace("-", "").lower() for v in (ebook_meta.get("isbn"), ebook_meta.get("asin")) if v}
    if epub_ids:
        for candidate in pool:
            cand_ids = {str(v).replace("-", "").lower() for v in (candidate.get("isbn"), candidate.get("asin")) if v}
            if epub_ids & cand_ids:
                return candidate, "identifier"

    # Tier 1 — strict fuzzy gate, then let the Ollama judge arbitrate the strong set.
    author_known = bool((ebook_meta.get("author") or "").strip())
    strong = [
        c for c in pool
        if c["title_sim"] >= 0.90 and (not author_known or (c.get("author_sim") or 0) >= 0.80)
    ]
    if not strong or len(strong) > 3:
        return None, None  # nothing strong, or too many rivals — leave for manual review

    try:
        ollama = _container.ollama_client()
    except Exception:
        ollama = None
    if not ollama or not ollama.is_configured():
        return None, None  # no Ollama → never auto-map on fuzzy alone

    epub_title = (ebook_meta.get("title") or "").strip()
    epub_author = (ebook_meta.get("author") or "").strip()
    conf_min = float(os.environ.get("OLLAMA_JUDGE_CONFIDENCE_MIN", 85))

    idx = judge_best_candidate(
        ollama,
        epub_title,
        epub_author,
        [{"title": c["title"], "author": c["author"]} for c in strong],
        conf_min,
    )
    if idx is None:
        return None, None
    best = strong[idx]

    # A non-strong rival with a near-equal title (e.g. same name, different author) is
    # genuine ambiguity the judge never saw — leave it for the user.
    strong_ids = {id(c) for c in strong}
    if any(
        id(c) not in strong_ids and c["title_sim"] >= best["title_sim"] - 0.05
        for c in pool
    ):
        return None, None

    # Volume guard: a base title fuzzy-matches its sequel well above threshold; the
    # trailing volume number must agree so we never auto-map the wrong book in a series.
    if _trailing_volume(epub_title) != _trailing_volume(best["title"]):
        return None, None
    return best, "agreement"


def _resolve_library_ebook_source(epub_filename):
    """Resolve a filesystem EPUB to its library identity (BookOrbit/Grimmory).

    The mapping must reference the library copy by source id so progress actually
    syncs to BookOrbit/Grimmory rather than treating it as a bare local file.
    Returns (source, source_id) or (None, None).
    """
    try:
        bookorbit = _container.bookorbit_client()
        if bookorbit and bookorbit.is_configured():
            match = bookorbit.find_book_by_filename(epub_filename, allow_refresh=False)
            if match and match.get("id") is not None:
                return "BookOrbit", str(match["id"])
    except Exception as e:
        logger.debug(f"Auto-map: BookOrbit filename resolve failed: {e}")
    try:
        booklore = _container.booklore_client()
        if booklore and booklore.is_configured():
            match = booklore.find_book_by_filename(epub_filename, allow_refresh=False)
            if match and match.get("id") is not None:
                return "BookLore", str(match["id"])
    except Exception as e:
        logger.debug(f"Auto-map: Grimmory filename resolve failed: {e}")
    return None, None


def _auto_map_ebook_to_audiobook(doc_hash_val, epub_filename, candidate, reason):
    """Create a full ebook↔audiobook mapping and link the KOSync document. Returns Book or None."""
    try:
        mapping_service = _container.book_mapping_service()
    except Exception as e:
        logger.warning(f"Auto-map: book mapping service unavailable: {e}")
        return None

    ebook_source, ebook_source_id = _resolve_library_ebook_source(epub_filename)

    saved = mapping_service.create_audio_mapping_from_match(
        audio_source="abs",
        audio_source_id=candidate["abs_id"],
        audio_title=candidate["title"],
        ebook_filename=epub_filename,
        audio_duration=candidate.get("duration") or None,
        audio_cover_url=f"/api/cover-proxy/{candidate['abs_id']}",
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
        booklore_ebook_id=ebook_source_id if ebook_source == "BookLore" else None,
        kosync_doc_id=doc_hash_val,
    )
    if not saved:
        return None

    try:
        _database_service.link_kosync_document(doc_hash_val, saved.abs_id)
    except Exception as e:
        logger.debug(f"Auto-map: link_kosync_document failed: {e}")
    try:
        _database_service.dismiss_suggestion(doc_hash_val)
    except Exception:
        pass

    ebook_label = f"{saved.ebook_source}:{saved.ebook_source_id}" if saved.ebook_source else "local file"
    logger.info(
        f"✅ Auto-mapped '{candidate['title']}' (abs {candidate['abs_id']}) ↔ '{epub_filename}' "
        f"[ebook={ebook_label}] via {reason} (title_sim={candidate.get('title_sim')}, author_sim={candidate.get('author_sim')})"
    )
    if _manager:
        try:
            _manager.run_sync_for_all_users(target_abs_id=saved.abs_id)
        except Exception as e:
            logger.debug(f"Auto-map: initial sync_cycle failed: {e}")
    return saved


def _record_user_kosync_state(book, percentage, progress, timestamp, user_id):
    """Persist a KoSync PUT into the per-user State table.

    The kosync_documents row is keyed only by document hash, so two users reading
    the same EPUB share that transient row. State is keyed by user and is the
    durable isolation boundary for subsequent GETs and sync cycles.
    """
    if not book or user_id is None:
        return
    try:
        _database_service.save_state(State(
            abs_id=book.abs_id,
            client_name="kosync",
            percentage=float(percentage or 0),
            timestamp=int(timestamp.timestamp()) if timestamp else int(time.time()),
            last_updated=int(time.time()),
            xpath=progress or "",
            user_id=user_id,
        ))
    except Exception as exc:
        logger.warning(
            "KOSync: failed to persist user-scoped state for '%s' user_id=%s: %s",
            getattr(book, "abs_id", None),
            user_id,
            exc,
        )


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
    now = utcnow()
    now_ts = time.time()
    _flush_stale_kosync_sessions(now_ts)

    kosync_doc = _database_service.get_kosync_document(doc_hash)
    if kosync_doc and kosync_doc.linked_abs_id:
        linked = _database_service.get_book(kosync_doc.linked_abs_id)
        if linked and not _kosync_user_may_access_book(linked):
            return jsonify({"error": "Document not found"}), 404

    # Optional "furthest wins" protection
    furthest_wins = os.environ.get('KOSYNC_FURTHEST_WINS', 'true').lower() == 'true'
    force_update = data.get('force', False)
    is_internal = _is_internal_kosync_device(device, device_id)
    request_user_id = getattr(g, 'kosync_user_id', None)

    # Allow rewinds if:
    # 1. Force flag is set (e.g. from SyncManager)
    # 2. Update comes from the SAME device (user moved slider back)
    # 3. Update is internal (sync-bot) — must reach debounce-clear logic below
    # Furthest-wins compares against THIS user's own last position
    # (kosync_user_progress), so one user's rewind is never judged against
    # another user's further position on the same shared hash. Falls back to the
    # shared row only for a single-user-no-accounts install (no per-user row).
    user_prog = _database_service.get_user_kosync_progress(doc_hash, request_user_id)
    baseline = user_prog
    if baseline is None and kosync_doc is not None:
        # Pre-migration / first PUT: use the shared row as baseline only when it
        # is this user's own or unstamped (legacy), never another user's.
        doc_user_id = getattr(kosync_doc, "user_id", None)
        if request_user_id is None or doc_user_id in (None, request_user_id):
            baseline = kosync_doc
    baseline_pct = float(baseline.percentage) if baseline and baseline.percentage else 0
    same_device = bool(baseline and baseline.device_id and baseline.device_id == device_id)

    if (
        furthest_wins
        and baseline_pct
        and not force_update
        and not same_device
        and not is_internal
    ):
        new_pct = float(percentage)
        if new_pct < baseline_pct - 0.0001:
            logger.info(f"KOSync: Ignored progress from '{device}' for doc {doc_hash} (user has higher: {baseline_pct:.2f}% vs new {new_pct:.2f}%)")
            return jsonify({
                "document": doc_hash,
                "timestamp": int(baseline.timestamp.timestamp()) if baseline and baseline.timestamp else int(now.timestamp())
            }), 200

    if kosync_doc is None:
        kosync_doc = KosyncDocument(
            document_hash=doc_hash,
            progress=progress,
            percentage=percentage,
            device=device,
            device_id=device_id,
            timestamp=now,
            user_id=request_user_id,
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
        if request_user_id is not None and not is_internal:
            kosync_doc.user_id = request_user_id

    _database_service.save_kosync_document(kosync_doc)
    if not is_internal:
        _record_recent_external_kosync_put(doc_hash, device, device_id, percentage, now_ts, request_user_id)
        # Per-user device progress: the durable per-user record for unlinked docs
        # and the furthest-wins / sibling-GET source (no-op for no-accounts installs).
        _database_service.upsert_user_kosync_progress(
            doc_hash, percentage, progress=progress, device=device,
            device_id=device_id, timestamp=now, user_id=request_user_id,
        )

    # Update linked book if exists
    linked_book = None
    if kosync_doc.linked_abs_id:
        linked_book = _database_service.get_book(kosync_doc.linked_abs_id)
    else:
        linked_book = _database_service.get_book_by_kosync_id(doc_hash)
        if not linked_book:
            # Mirror the GET path: an unlinked device hash may belong to an
            # already-mapped book (e.g. a different EPUB build of the same title),
            # resolvable via a filename sibling. Try that before falling through to
            # auto-discovery so we link instead of creating a duplicate mapping.
            linked_book = _resolve_book_by_sibling_hash(doc_hash, existing_doc=kosync_doc)
        if linked_book:
            _register_hash_for_book(doc_hash, linked_book)

    if linked_book and not is_internal:
        if not _kosync_user_may_access_book(linked_book):
            return jsonify({"error": "Document not found"}), 404
        _record_user_kosync_state(linked_book, percentage, progress, now, request_user_id)

    # AUTO-DISCOVERY
    if not linked_book:
        if not _is_valid_hex_hash(doc_hash):
            logger.warning(
                f"⚠️ KOSync: Rejected malformed document ID: '{doc_hash}' (PUT from {request.remote_addr})"
            )
        else:
            _schedule_auto_discovery(
                doc_hash,
                user_id=request_user_id,
                source="put",
            )

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
                cleared = [k for k in _kosync_debounce if k[0] == linked_book.abs_id]
                for k in cleared:
                    del _kosync_debounce[k]
                if cleared:
                    logger.debug(f"KOSync PUT: Cleared pending debounce for internal update on '{linked_book.abs_title}'")
        if linked_book.status == 'active' and _manager and not is_internal and instant_sync_enabled:
            logger.debug(f"KOSync PUT: Progress event recorded for '{linked_book.abs_title}'")
            _record_kosync_event(linked_book.abs_id, linked_book.abs_title, user_id=getattr(g, 'kosync_user_id', None))

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
    """Return the optional KOReader managed-folder sync manifest.

    The response is always served from a pre-built cache maintained by
    _manifest_prebuilder_loop(), so this endpoint returns in <200 ms.
    On a cold start (cache not yet populated) it falls back to an inline
    build and primes the cache so subsequent requests are instant.
    """
    global _manifest_cache
    _start_manifest_prebuilder()

    user_id = getattr(g, "kosync_user_id", None)

    with _manifest_cache_lock:
        cached = _manifest_cache

    if cached is not None:
        return jsonify(_scope_manifest_to_user(cached, user_id)), 200

    # Cold start: cache not yet populated — build inline and prime the cache.
    service = _get_koreader_device_sync_service()
    if not service:
        return jsonify({"error": "Device sync service unavailable"}), 503

    manifest = service.build_manifest()
    with _manifest_cache_lock:
        _manifest_cache = manifest

    return jsonify(_scope_manifest_to_user(manifest, user_id)), 200


@kosync_sync_bp.route('/device-sync/books/<path:abs_id>/download', methods=['GET'])
@kosync_sync_bp.route('/koreader/device-sync/books/<path:abs_id>/download', methods=['GET'])
@kosync_auth_required
def koreader_device_sync_download(abs_id):
    """Download the original ebook for a bridge-managed KOReader sync item."""
    service = _get_koreader_device_sync_service()
    if not service:
        return jsonify({"error": "Device sync service unavailable"}), 503

    # Membership gate: a device may only download books its user has claimed.
    # Deny only when the book is claimed by other users and not this one (a book
    # with no claims stays downloadable for back-compat). 404 (not 403) so a
    # foreign abs_id is indistinguishable from a missing one.
    user_id = getattr(g, "kosync_user_id", None)
    if user_id is not None and _database_service is not None:
        claimants = _database_service.get_book_user_ids(abs_id)
        # Require an explicit claim: a device may only download books its user has
        # matched (an unclaimed book is on nobody's manifest and is not served).
        if user_id not in claimants:
            return jsonify({"error": "Book not available"}), 404

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


@kosync_sync_bp.route('/device-sync/statistics', methods=['POST'])
@kosync_sync_bp.route('/koreader/device-sync/statistics', methods=['POST'])
@kosync_auth_required
def koreader_upload_statistics():
    """Receive incremental KOReader reading statistics uploads."""
    data = request.json
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    books = data.get("books")
    page_stats = data.get("page_stats")
    if not isinstance(books, list) or not isinstance(page_stats, list):
        return jsonify({"error": "Expected 'books' and 'page_stats' arrays"}), 400
    if len(books) > _KOREADER_STATS_MAX_BOOKS:
        return jsonify({"error": f"Too many books in statistics upload (max {_KOREADER_STATS_MAX_BOOKS})"}), 413
    if len(page_stats) > _KOREADER_STATS_MAX_PAGE_STATS:
        return jsonify({"error": f"Too many page_stats in statistics upload (max {_KOREADER_STATS_MAX_PAGE_STATS})"}), 413

    device = str(data.get("device") or "").strip()
    device_id = str(data.get("device_id") or "").strip()
    device_key = (device_id or device).strip()
    if not device_key:
        return jsonify({"error": "Missing device identity"}), 400

    if not _database_service:
        return jsonify({"error": "Database service unavailable"}), 503
    user_id = getattr(g, "kosync_user_id", None)

    def _persist():
        accepted = _database_service.upsert_koreader_book_stats(
            device=device,
            device_id=device_id,
            books=books,
            user_id=user_id,
        )
        result = _database_service.bulk_insert_koreader_page_stats(
            device=device,
            device_id=device_id,
            page_stats=page_stats,
            user_id=user_id,
        )
        return accepted, result

    # A long sync cycle can hold the SQLite writer past the busy timeout, so a
    # single "database is locked" shouldn't surface as a 500. Retry with
    # exponential backoff before giving up (see issue #315).
    max_attempts = 3

    accepted_books = None
    page_insert_result = None
    for attempt in range(1, max_attempts + 1):
        try:
            accepted_books, page_insert_result = _persist()
            break
        except Exception as e:
            is_locked = "database is locked" in str(e).lower()
            if is_locked and attempt < max_attempts:
                backoff = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "KOReader statistics write for device '%s' hit a locked database "
                    "(attempt %d/%d); retrying in %.1fs",
                    device_key, attempt, max_attempts, backoff,
                )
                time.sleep(backoff)
                continue
            logger.error("KOReader statistics upload failed for device '%s': %s", device_key, e)
            return jsonify({"error": "Failed to persist statistics upload"}), 500

    return jsonify({
        "accepted_books": int(accepted_books or 0),
        "accepted_page_stats": int(page_insert_result.get("accepted") or 0),
        "duplicate_page_stats": int(page_insert_result.get("duplicates") or 0),
        "echoed_page_stats": int(page_insert_result.get("echoes") or 0),
    }), 200


@kosync_sync_bp.route('/device-sync/statistics/merged', methods=['GET'])
@kosync_sync_bp.route('/koreader/device-sync/statistics/merged', methods=['GET'])
@kosync_auth_required
def koreader_merged_statistics():
    """Return other devices' page-stat events so the plugin can merge them locally."""
    if os.environ.get("KOREADER_COMBINE_DEVICE_STATS", "true").lower() != "true":
        return jsonify({"enabled": False, "page_stats": []}), 200

    device = str(request.args.get("device") or "").strip()
    device_id = str(request.args.get("device_id") or "").strip()
    device_key = (device_id or device).strip()
    if not device_key:
        return jsonify({"error": "Missing device identity"}), 400

    if not _database_service:
        return jsonify({"error": "Database service unavailable"}), 503

    since = None
    since_raw = request.args.get("since")
    if since_raw:
        try:
            since = float(since_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid 'since' value"}), 400
    limit = _KOREADER_STATS_MERGE_LIMIT
    limit_raw = request.args.get("limit")
    if limit_raw:
        try:
            limit = max(min(int(limit_raw), _KOREADER_STATS_MERGE_LIMIT), 1)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid 'limit' value"}), 400

    try:
        user_id = getattr(g, "kosync_user_id", None)
        merged = _database_service.get_merged_koreader_page_stats(
            exclude_device_key=device_key,
            since=since,
            user_id=user_id,
            limit=limit,
        )
        page_stats = merged.get("page_stats") or []
        md5s = {row["md5"] for row in page_stats}
        books_meta = (
            _database_service.get_merged_koreader_book_meta(device_key, md5s, user_id=user_id) if md5s else []
        )
    except Exception as e:
        logger.error("KOReader merged statistics fetch failed for device '%s': %s", device_key, e)
        return jsonify({"error": "Failed to fetch merged statistics"}), 500

    return jsonify({
        "enabled": True,
        "page_stats": page_stats,
        "books": books_meta,
        "watermark": merged.get("watermark"),
        "truncated": bool(merged.get("truncated")),
    }), 200


def _annotation_sync_enabled() -> bool:
    return os.environ.get("KOREADER_ANNOTATION_SYNC", "true").strip().lower() in ("true", "1", "yes", "on")


@kosync_sync_bp.route('/device-sync/annotations/exchange', methods=['POST'])
@kosync_sync_bp.route('/koreader/device-sync/annotations/exchange', methods=['POST'])
@kosync_auth_required
def koreader_exchange_annotations():
    """Two-way highlight/annotation exchange for one device.

    Body mirrors the exchange convention the BookOrbit koplugin established:
    ``{device, device_id, books: [{hash, keys: [{k, dt}], keysComplete,
    changes: [...]}]}``. The response returns this device's pending delta per
    book: ``{books: [{hash, toApply: {add, edit, delete}, more}]}``. ``more``
    tells paged clients to acknowledge this batch and immediately pull the
    remainder. The device reports applied changes via the exchange-ack endpoint.
    """
    if not _annotation_sync_enabled():
        return jsonify({"enabled": False, "books": []}), 200

    data = request.json
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400
    books = data.get("books")
    if not isinstance(books, list) or not books:
        return jsonify({"error": "Expected non-empty 'books' array"}), 400
    if len(books) > 20:
        return jsonify({"error": "Too many books per exchange (max 20)"}), 400

    device = str(data.get("device") or "").strip()
    device_id = str(data.get("device_id") or "").strip()
    device_key = (device_id or device).strip()
    if not device_key:
        return jsonify({"error": "Missing device identity"}), 400

    if not _database_service:
        return jsonify({"error": "Database service unavailable"}), 503

    try:
        result = _database_service.exchange_koreader_annotations(
            user_id=g.kosync_user_id,
            device_key=device_key,
            books=books,
        )
    except Exception as e:
        logger.error("KOReader annotation exchange failed for device '%s': %s", device_key, e)
        return jsonify({"error": "Annotation exchange failed"}), 500

    result["enabled"] = True
    return jsonify(result), 200


@kosync_sync_bp.route('/device-sync/annotations/exchange-ack', methods=['POST'])
@kosync_sync_bp.route('/koreader/device-sync/annotations/exchange-ack', methods=['POST'])
@kosync_auth_required
def koreader_exchange_annotations_ack():
    """Record which exchanged annotations the device actually applied/deleted."""
    if not _annotation_sync_enabled():
        return jsonify({"enabled": False, "acked": 0}), 200

    data = request.json
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400
    books = data.get("books")
    if not isinstance(books, list):
        return jsonify({"error": "Expected 'books' array"}), 400

    device = str(data.get("device") or "").strip()
    device_id = str(data.get("device_id") or "").strip()
    device_key = (device_id or device).strip()
    if not device_key:
        return jsonify({"error": "Missing device identity"}), 400

    if not _database_service:
        return jsonify({"error": "Database service unavailable"}), 503

    try:
        result = _database_service.ack_koreader_annotations(
            user_id=g.kosync_user_id,
            device_key=device_key,
            books=books,
        )
    except Exception as e:
        logger.error("KOReader annotation ack failed for device '%s': %s", device_key, e)
        return jsonify({"error": "Annotation ack failed"}), 500

    result["enabled"] = True
    return jsonify(result), 200


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
    results = []

    def record_result(index: int, session: dict, was_accepted: bool,
                      reason: str = None) -> None:
        """Record one ordered acknowledgement for the plugin's local queue."""
        result = {"index": index, "accepted": was_accepted}
        session_id = session.get('session_id') if isinstance(session, dict) else None
        if session_id:
            result["session_id"] = session_id
        if reason:
            result["reason"] = reason
        results.append(result)

    for index, session in enumerate(data, start=1):
        if not isinstance(session, dict):
            logger.warning("Session upload: invalid session at index %d", index)
            rejected += 1
            record_result(index, {}, False, "invalid_session")
            continue

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
            record_result(index, session, False, "book_not_found")
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

        # A plugin keeps its local queue until SQLite acknowledges the remote
        # upload. If that acknowledgement fails, the same payload is retried.
        # Treat an identical, already-recorded plugin session as accepted so the
        # retry is idempotent and does not inflate reading statistics.
        already_recorded = False
        if _database_service:
            try:
                already_recorded = _database_service.has_matching_reading_session(
                    abs_id=abs_id,
                    session_type=session_type,
                    start_time=float(start_time),
                    end_time=float(end_time),
                    duration_seconds=int(duration_seconds),
                    start_progress=start_progress,
                    end_progress=end_progress,
                    leader_client="BridgeSync_Plugin",
                ) is True
            except Exception as e:
                logger.warning(
                    "Session upload: duplicate check failed for '%s': %s",
                    abs_id,
                    e,
                )
        if already_recorded:
            logger.info("Session upload: accepted duplicate retry for '%s'", abs_id)
            accepted += 1
            record_result(index, session, True)
            continue

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
            record_result(index, session, False, "record_failed")
            continue

        record_result(index, session, True)

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

        # Forward to BookOrbit if the ebook is hosted there
        _forward_reading_session_to_bookorbit(
            book, start_time, end_time, start_progress, end_progress, book_type=session_type
        )

    logger.info(f"Session upload: accepted={accepted}, rejected={rejected}")
    return jsonify({"accepted": accepted, "rejected": rejected, "results": results}), 200


@kosync_sync_bp.route('/device-sync/logs', methods=['POST'])
@kosync_sync_bp.route('/koreader/device-sync/logs', methods=['POST'])
@kosync_auth_required
def kosync_upload_device_logs():
    """Relay a bounded BridgeSync log tail into the bridge's Docker logs."""
    if request.content_length and request.content_length > _BRIDGESYNC_LOG_MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Log payload is too large"}), 413

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected JSON object"}), 400

    raw_lines = data.get("lines", [])
    if not isinstance(raw_lines, list):
        return jsonify({"error": "lines must be an array"}), 400

    def clean_field(value, limit: int) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ")
        return " ".join(text.split())[:limit]

    operation = clean_field(data.get("operation"), 64) or "unknown"
    status = clean_field(data.get("status"), 16).lower()
    if status not in {"success", "partial", "failure"}:
        status = "unknown"
    device = clean_field(data.get("device"), 128) or "KOReader"
    device_id = clean_field(data.get("device_id"), 128) or "unknown"
    plugin_version = clean_field(data.get("plugin_version"), 32) or "unknown"
    user_id = getattr(g, "kosync_user_id", None)

    accepted_lines = []
    for raw_line in raw_lines[:_BRIDGESYNC_LOG_MAX_LINES]:
        if not isinstance(raw_line, (str, int, float, bool)):
            continue
        line = clean_field(raw_line, _BRIDGESYNC_LOG_MAX_LINE_CHARS)
        if line:
            accepted_lines.append(line)

    report_logger = (
        logger.error if status == "failure"
        else logger.warning if status == "partial"
        else logger.info
    )
    report_logger(
        "BridgeSync device report: user_id=%s device=%s device_id=%s plugin=%s "
        "operation=%s status=%s lines=%d",
        user_id,
        device,
        device_id,
        plugin_version,
        operation,
        status,
        len(accepted_lines),
    )

    prefix = f"BridgeSync device log [{device}/{device_id}] [{operation}]"
    for line in accepted_lines:
        lowered = line.lower()
        if "[error]" in lowered or "[err]" in lowered:
            logger.error("%s %s", prefix, line)
        elif "[warn]" in lowered or "[warning]" in lowered:
            logger.warning("%s %s", prefix, line)
        else:
            logger.info("%s %s", prefix, line)

    return jsonify({"accepted": len(accepted_lines)}), 200


# ── Plugin self-update helpers ──

def _parse_meta_lua_version(meta_path: Path) -> Optional[str]:
    """Extract the version string from a Lua table file of the form: version = "x.y.z"."""
    try:
        content = meta_path.read_text(encoding="utf-8")
        m = re.search(r'version\s*=\s*"([^"]+)"', content)
        if m:
            return m.group(1)
    except OSError:
        pass
    return None


def _get_plugin_dir_max_mtime(plugin_dir: Path) -> float:
    """Return the latest mtime across all files in plugin_dir (recursive)."""
    max_mtime = 0.0
    for path in plugin_dir.rglob("*"):
        if path.is_file():
            mtime = path.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
    return max_mtime


def _build_plugin_zip(plugin_dir: Path) -> bytes:
    """Build an in-memory zip of plugin_dir with bridgesync.koplugin/ as the top-level folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(plugin_dir.rglob("*")):
            if not path.is_file():
                continue
            parts = path.parts
            if "__pycache__" in parts or ".git" in parts:
                continue
            if path.name.endswith(".pyc"):
                continue
            arcname = "bridgesync.koplugin/" + str(path.relative_to(plugin_dir))
            zf.write(str(path), arcname)
    return buf.getvalue()


@kosync_sync_bp.route('/device-sync/plugin/version', methods=['GET'])
@kosync_sync_bp.route('/koreader/device-sync/plugin/version', methods=['GET'])
@kosync_auth_required
def koreader_plugin_version():
    """Return the current BridgeSync plugin version from _meta.lua."""
    logger.debug("Plugin directory path: %s (exists: %s)", _PLUGIN_DIR, _PLUGIN_DIR.is_dir())
    if not _PLUGIN_DIR.is_dir():
        return jsonify({"error": "Plugin directory not found"}), 404

    version = _parse_meta_lua_version(_PLUGIN_DIR / "_meta.lua")
    if not version:
        return jsonify({"error": "Could not determine plugin version"}), 404

    return jsonify({"version": version, "name": "bridgesync"}), 200


@kosync_sync_bp.route('/device-sync/plugin/download', methods=['GET'])
@kosync_sync_bp.route('/koreader/device-sync/plugin/download', methods=['GET'])
@kosync_auth_required
def koreader_plugin_download():
    """Serve the BridgeSync plugin as a zip archive; cached until any file's mtime changes."""
    global _plugin_zip_cache

    if not _PLUGIN_DIR.is_dir():
        return jsonify({"error": "Plugin directory not found"}), 404

    version = _parse_meta_lua_version(_PLUGIN_DIR / "_meta.lua")
    if not version:
        return jsonify({"error": "Could not determine plugin version"}), 404

    current_mtime = _get_plugin_dir_max_mtime(_PLUGIN_DIR)

    with _plugin_zip_cache_lock:
        if _plugin_zip_cache is None or _plugin_zip_cache[1] < current_mtime:
            logger.info("Plugin zip cache miss — rebuilding bridgesync plugin archive")
            zip_bytes = _build_plugin_zip(_PLUGIN_DIR)
            _plugin_zip_cache = (zip_bytes, current_mtime)
        zip_bytes = _plugin_zip_cache[0]

    filename = f"bridgesync-{version}.zip"
    return send_file(
        io.BytesIO(zip_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


@kosync_admin_bp.route('/api/kosync-plugin/version', methods=['GET'])
def admin_plugin_version():
    """Return the BridgeSync plugin version for the settings-page download card."""
    if not _PLUGIN_DIR.is_dir():
        return jsonify({"error": "Plugin directory not found"}), 404

    version = _parse_meta_lua_version(_PLUGIN_DIR / "_meta.lua")
    if not version:
        return jsonify({"error": "Could not determine plugin version"}), 404

    return jsonify({"version": version, "name": "bridgesync"}), 200


@kosync_admin_bp.route('/api/kosync-plugin/download', methods=['GET'])
def admin_plugin_download():
    """Serve the BridgeSync plugin zip to the browser (settings-page download).

    Unlike the device self-update route on kosync_sync_bp, this is a same-origin
    dashboard route with no KOSync device auth, so the settings page can link to
    it directly. Shares the plugin zip cache with the device-facing endpoint.
    """
    global _plugin_zip_cache

    if not _PLUGIN_DIR.is_dir():
        return jsonify({"error": "Plugin directory not found"}), 404

    version = _parse_meta_lua_version(_PLUGIN_DIR / "_meta.lua")
    if not version:
        return jsonify({"error": "Could not determine plugin version"}), 404

    current_mtime = _get_plugin_dir_max_mtime(_PLUGIN_DIR)

    with _plugin_zip_cache_lock:
        if _plugin_zip_cache is None or _plugin_zip_cache[1] < current_mtime:
            logger.info("Plugin zip cache miss — rebuilding bridgesync plugin archive")
            zip_bytes = _build_plugin_zip(_PLUGIN_DIR)
            _plugin_zip_cache = (zip_bytes, current_mtime)
        zip_bytes = _plugin_zip_cache[0]

    return send_file(
        io.BytesIO(zip_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"bridgesync-{version}.zip",
    )


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


def _clear_stale_kosync_metadata(cached_doc, filename=None, booklore_id=None):
    """Remove lookup metadata from an old hash row while preserving progress/link data."""
    if not cached_doc:
        return

    changed = False
    if filename is not None and cached_doc.filename == filename:
        cached_doc.filename = None
        cached_doc.mtime = None
        cached_doc.source = None
        changed = True
    if booklore_id is not None and str(cached_doc.booklore_id) == str(booklore_id):
        cached_doc.booklore_id = None
        cached_doc.source = None
        changed = True

    if changed:
        _database_service.save_kosync_document(cached_doc)


def _cache_kosync_metadata(document_hash, filename, source, mtime=None, booklore_id=None, cached_doc=None):
    """Cache hash metadata without mutating a primary key into an existing hash."""
    existing = _database_service.get_kosync_document(document_hash)
    if existing:
        existing.filename = filename
        existing.source = source
        if mtime is not None:
            existing.mtime = mtime
        if booklore_id is not None:
            existing.booklore_id = str(booklore_id)
        saved = _database_service.save_kosync_document(existing)
        if cached_doc and cached_doc.document_hash != document_hash:
            _clear_stale_kosync_metadata(cached_doc, filename=filename, booklore_id=booklore_id)
        return saved

    if cached_doc and cached_doc.document_hash == document_hash:
        cached_doc.filename = filename
        cached_doc.source = source
        if mtime is not None:
            cached_doc.mtime = mtime
        if booklore_id is not None:
            cached_doc.booklore_id = str(booklore_id)
        return _database_service.save_kosync_document(cached_doc)

    _upsert_kosync_metadata(document_hash, filename, source, mtime=mtime, booklore_id=booklore_id)
    if cached_doc:
        _clear_stale_kosync_metadata(cached_doc, filename=filename, booklore_id=booklore_id)
    return _database_service.get_kosync_document(document_hash)


def _ebook_search_dirs() -> list:
    """Directories to scan for ebook files: BOOKS_DIR plus EXTRA_EBOOK_DIRS libraries."""
    dirs = []
    if _ebook_dir:
        dirs.append(_ebook_dir)
    try:
        dirs.extend(_container.ebook_parser().extra_book_dirs)
    except Exception as e:
        logger.debug(f"KOSync: could not read extra ebook dirs: {e}")
    return dirs


def _scan_directory_for_hash(scan_dir, doc_hash: str) -> Optional[str]:
    """Hash-scan one directory's *.epub files for doc_hash. Returns filename or None."""
    if not scan_dir or not scan_dir.exists():
        return None
    logger.info(f"🔎 Starting filesystem search in {scan_dir} for hash {doc_hash}...")
    count = 0
    for epub_path in scan_dir.rglob("*.epub"):
        count += 1
        if count % 100 == 0:
            logger.debug(f"Checked {count} local EPUBs...")

        # Optimization: Check if we already have this file's hash in DB
        cached_doc = _database_service.get_kosync_doc_by_filename(epub_path.name)
        if cached_doc:
            current_mtime = epub_path.stat().st_mtime
            if cached_doc.mtime == current_mtime:
                if cached_doc.document_hash == doc_hash:
                    logger.info(f"📚 Matched EPUB via DB filename lookup: {epub_path.name}")
                    return epub_path.name
                continue

        try:
            computed_hash = _container.ebook_parser().get_kosync_id(epub_path)
            _cache_kosync_metadata(
                computed_hash,
                epub_path.name,
                'filesystem',
                mtime=epub_path.stat().st_mtime,
                cached_doc=cached_doc,
            )
            if computed_hash == doc_hash:
                logger.info(f"📚 Matched EPUB via filesystem: {epub_path.name}")
                return epub_path.name
        except Exception as e:
            logger.debug(f"Error checking file {epub_path.name}: {e}")
    logger.info(f"🔍 Filesystem search in {scan_dir} finished. Checked {count} files. No match found")
    return None


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
        
        # Check for a valid linked book with the original filename.
        if doc and doc.linked_abs_id:
             book = _database_service.get_book(doc.linked_abs_id)
             if book and book.original_ebook_filename:
                 try:
                     _container.ebook_parser().resolve_book_path(book.original_ebook_filename)
                     logger.info(f"📚 Matched EPUB via Linked Book Original Filename: {book.original_ebook_filename}")
                     return book.original_ebook_filename
                 except Exception:
                     pass

        # Check filesystem — BOOKS_DIR plus any EXTRA_EBOOK_DIRS libraries.
        for scan_dir in _ebook_search_dirs():
            matched = _scan_directory_for_hash(scan_dir, doc_hash)
            if matched:
                return matched

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
                                cache_path = safe_cache_path(cache_dir, safe_title)
                                if cache_path is None:
                                    logger.warning("KOSync: refused unsafe cache filename '%s'", safe_title)
                                    continue
                                with open(cache_path, 'wb') as f:
                                    f.write(book_content)
                                logger.info(f"📥 Persisted Grimmory book to cache: {safe_title}")

                                # Save/Update KosyncDocument in DB
                                _cache_kosync_metadata(
                                    computed_hash,
                                    safe_title,
                                    'booklore',
                                    booklore_id=book_id,
                                    cached_doc=cached_doc,
                                )

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
    # get_states_for_book resolves the authenticated user from the ambient context
    # (set by kosync_auth_required), so states are already user-scoped.
    states = _database_service.get_states_for_book(book.abs_id)

    # The bridge-synced KoSync position for this book — where ABS/Storyteller/etc.
    # have placed it. A device's own reported position is only honored when it is
    # AHEAD of this (see the furthest-wins gate below); otherwise the device is
    # behind a cross-source sync and must be pulled forward.
    kosync_state = (
        next((s for s in states if s.client_name.lower() == 'kosync'), None)
        if states else None
    )
    synced_pct = float(kosync_state.percentage) if kosync_state and kosync_state.percentage else 0.0

    # Also check this user's per-user progress across the book's hashes — so a
    # device position that advanced past the synced State (e.g. a different EPUB
    # build of the same title) is honored, without leaking another user's
    # position on the same shared title.
    user_id = getattr(g, "kosync_user_id", None)
    progress_rows = _database_service.get_user_kosync_progress_for_book(book.abs_id, user_id)
    if not progress_rows:
        # Fallback: shared sibling docs (legacy / pre-migration), scoped to this
        # user's own or unstamped (legacy) rows so no cross-user position leaks.
        sibling_docs = _database_service.get_kosync_documents_for_book(book.abs_id)
        if user_id is not None:
            sibling_docs = [d for d in sibling_docs if getattr(d, "user_id", None) in (None, user_id)]
        progress_rows = sibling_docs
    docs_with_progress = [
        d for d in progress_rows
        if d.percentage and float(d.percentage) > 0 and (d.progress or "").strip()
    ]
    if docs_with_progress:
        best_doc = max(docs_with_progress, key=lambda d: float(d.percentage))
        # Furthest-wins: only hand back the device's own position when it is genuinely
        # ahead of the bridge-synced position. The bridge's internal sync-push advances
        # the synced State but not this per-user row, so a device that is *behind* (the
        # book was just advanced from ABS/Storyteller/etc.) must be pulled forward —
        # returning its stale spot here drags the reader back and starts a GET/PUT
        # tug-of-war (e.g. a 40% audiobook position repeatedly snapping back to 9%).
        # An ahead sibling still wins. An equal sibling is only a locator fallback
        # when the synced State has no viable locator of its own; otherwise an old
        # sibling XPath from another EPUB build could replace the authoritative
        # synced locator at the same percentage.
        sibling_pct = float(best_doc.percentage)
        synced_has_locator = bool(
            kosync_state
            and (
                str(getattr(kosync_state, "xpath", "") or "").strip()
                or str(getattr(kosync_state, "cfi", "") or "").strip()
            )
        )
        sibling_is_ahead = sibling_pct > synced_pct + 0.0001
        sibling_is_equal_fallback = (
            abs(sibling_pct - synced_pct) <= 0.0001
            and not synced_has_locator
        )
        if sibling_is_ahead or sibling_is_equal_fallback:
            logger.info(f"KOSync: Resolved {doc_id} to '{book.abs_title}' via sibling hash {best_doc.document_hash} ({float(best_doc.percentage):.2%})")
            poison_pill = _suppress_empty_progress_response(doc_id, float(best_doc.percentage), best_doc.progress)
            if poison_pill is not None:
                return poison_pill
            response_data = {
                "device": "abs-kosync-bridge",
                "device_id": "abs-kosync-bridge",
                "document": doc_id,
                "percentage": float(best_doc.percentage),
                "progress": best_doc.progress or "",
                "timestamp": int(best_doc.timestamp.timestamp()) if best_doc.timestamp else 0
            }
            response_data.update(
                _recent_external_kosync_put_metadata(
                    best_doc.document_hash,
                    response_data["percentage"],
                    user_id,
                )
            )
            return jsonify(response_data), 200

    if not states:
        return jsonify({"message": "Document not found on server"}), 404

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


_epub_identifier_cache: dict = {}


def _epub_identifiers_for(filename: str) -> set:
    """Cached read of an ebook's embedded DC identifiers (keyed by filename)."""
    if filename in _epub_identifier_cache:
        return _epub_identifier_cache[filename]
    try:
        ids = _container.ebook_parser().get_book_identifiers(filename)
    except Exception as e:
        logger.debug(f"KOSync: identifier read failed for '{filename}': {e}")
        ids = set()
    _epub_identifier_cache[filename] = ids
    return ids


def _resolve_book_by_epub_identifier(epub_filename: str, doc_id: str = None):
    """Link a hash-discovered library file to an existing mapping by a shared EPUB
    identifier.

    Handles a raw library file vs the re-stamped copy of the same work (different
    bytes and filename, but the same embedded Calibre/ISBN id) — e.g. a Kindle/Kobo
    reading the raw Calibre file while the bridge matched the re-stamped CWA copy.
    Scoped to the document's owning user.
    """
    found_ids = _epub_identifiers_for(epub_filename)
    if not found_ids:
        return None

    user_id = None
    if doc_id:
        doc = _database_service.get_kosync_document(doc_id)
        user_id = getattr(doc, "user_id", None) if doc else None

    try:
        candidates = _database_service.get_books_by_status("active", user_id=user_id)
    except TypeError:
        candidates = _database_service.get_books_by_status("active")

    for book in candidates or []:
        book_file = book.ebook_filename or book.original_ebook_filename
        if not book_file or book_file == epub_filename:
            continue
        if found_ids & _epub_identifiers_for(book_file):
            logger.info(
                f"🔗 KOSync: Matched '{epub_filename}' to '{book.abs_title}' via shared EPUB identifier"
            )
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


def _is_valid_hex_hash(value: str) -> bool:
    """Return True if value is a 32-character lowercase or uppercase hex string."""
    if len(value) != 32:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _schedule_auto_discovery(doc_id: str, user_id=None, *, source: str = "get") -> None:
    """Attempt to schedule a bounded auto-discovery scan for doc_id.

    Returns immediately if the hash is already being scanned, the queue is full,
    or AUTO_CREATE_EBOOK_MAPPING is off. Preserves _active_scans dedupe.
    """
    global _queued_discovery_count
    auto_create = os.environ.get('AUTO_CREATE_EBOOK_MAPPING', 'true').lower() == 'true'
    if not auto_create:
        return
    with _active_scans_lock:
        if doc_id in _active_scans:
            return
    with _queued_discovery_lock:
        if _queued_discovery_count >= _MAX_QUEUED_DISCOVERY:
            logger.warning(
                "KOSync: %s-discovery for %s dropped — discovery queue full (%d/%d)",
                source, doc_id, _queued_discovery_count, _MAX_QUEUED_DISCOVERY,
            )
            return

    # Create stub and register BEFORE submitting so concurrent requests for the
    # same hash skip immediately.
    from src.db.models import KosyncDocument as KD
    existing = _database_service.get_kosync_document(doc_id)
    if not existing:
        stub = KD(document_hash=doc_id, user_id=user_id)
        _database_service.save_kosync_document(stub)

    with _active_scans_lock:
        if doc_id in _active_scans:
            return
        _active_scans.add(doc_id)

    with _queued_discovery_lock:
        _queued_discovery_count += 1

    def _wrapped():
        global _queued_discovery_count
        try:
            if source == "get":
                _run_get_auto_discovery(doc_id)
            else:
                _run_put_auto_discovery_inner(doc_id, user_id)
        finally:
            with _active_scans_lock:
                _active_scans.discard(doc_id)
            with _queued_discovery_lock:
                _queued_discovery_count = max(0, _queued_discovery_count - 1)

    logger.info(
        "🔍 KOSync: Starting %s background discovery for %s",
        source, doc_id,
    )
    _discovery_executor.submit(_wrapped)


def _run_get_auto_discovery(doc_id: str) -> None:
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

        # Try to find an existing book that uses this epub — by filename, then by
        # shared EPUB identifier (raw library file vs the re-stamped matched copy).
        book = (
            _database_service.get_book_by_ebook_filename(epub_filename)
            or _resolve_book_by_epub_identifier(epub_filename, doc_id=doc_id)
        )
        if book:
            _database_service.link_kosync_document(doc_id, book.abs_id)
            logger.info(f"✅ KOSync: GET-discovery linked {doc_id} to '{book.abs_title}'")
            return

        logger.info(f"🔍 KOSync: GET-discovery found epub '{epub_filename}' but no matching book")
    except Exception as e:
        logger.error(f"❌ Error in GET auto-discovery: {e}")


def _run_put_auto_discovery_inner(doc_hash_val: str, user_id=None) -> None:
    """Discover and map an EPUB submitted by an authenticated KoSync user."""
    from src.db.models import Book, PendingSuggestion

    logger.info("KOSync: Scheduled auto-discovery for unmapped document %s", doc_hash_val)
    epub_filename = _try_find_epub_by_hash(doc_hash_val)
    if not epub_filename:
        return
    existing_book = (
        _database_service.get_book_by_ebook_filename(epub_filename)
        or _resolve_book_by_epub_identifier(epub_filename, doc_id=doc_hash_val)
    )
    if existing_book:
        _register_hash_for_book(doc_hash_val, existing_book)
        _database_service.dismiss_suggestion(doc_hash_val)
        return

    title = Path(epub_filename).stem
    try:
        ebook_meta = _container.ebook_parser().get_book_metadata(epub_filename) or {}
    except Exception as exc:
        logger.debug("Auto-discovery: EPUB metadata read failed: %s", exc)
        ebook_meta = {}
    ebook_meta.setdefault("title", title)
    candidates = _autodiscovery_audiobook_candidates(epub_filename, ebook_meta)
    if candidates and os.environ.get('KOSYNC_AUTO_MAP_ON_AGREEMENT', 'true').lower() == 'true':
        chosen, reason = _select_auto_map_candidate(ebook_meta, candidates)
        if chosen and _auto_map_ebook_to_audiobook(doc_hash_val, epub_filename, chosen, reason):
            return
    matches = [
        {
            "source": "abs", "abs_id": c["abs_id"], "title": c["title"],
            "author": c["author"], "duration": c["duration"],
            "confidence": "high" if c["title_sim"] >= 0.85 else "medium",
        }
        for c in candidates if (c.get("progress_pct") or 0) <= 75
    ]
    if matches:
        if not _database_service.suggestion_exists(doc_hash_val):
            matches.append({"source": "ebook", "filename": epub_filename, "confidence": "high"})
            _database_service.save_pending_suggestion(PendingSuggestion(
                source_id=doc_hash_val,
                title=ebook_meta.get("title") or title,
                author=ebook_meta.get("author"),
                cover_url=f"/api/cover-proxy/{matches[0]['abs_id']}",
                matches_json=json.dumps(matches),
            ))
        return
    book_id = f"ebook-{doc_hash_val[:16]}"
    _database_service.save_book(Book(
        abs_id=book_id, abs_title=title, ebook_filename=epub_filename,
        kosync_doc_id=doc_hash_val, status='active', sync_mode='ebook_only',
    ))
    _database_service.link_kosync_document(doc_hash_val, book_id)
    _database_service.dismiss_suggestion(doc_hash_val)
    if user_id is not None:
        _database_service.link_user_book(user_id, book_id)
    if _manager:
        _manager.run_sync_for_all_users(target_abs_id=book_id)


# ---------------- KOSync Document Management API ----------------

def _suppress_empty_progress_response(doc_id: str, percentage: float, progress: Optional[str]):
    safe_progress = progress.strip() if isinstance(progress, str) else ""
    if percentage > 0 and not safe_progress:
        logger.warning(
            "KOSync: Suppressing response for %s - percentage %.2f%% but no locator available. Returning 404 to prevent page-0 reset.",
            doc_id,
            percentage * 100.0,
        )
        return jsonify({"message": "Document not found on server"}), 404
    return None

@kosync_admin_bp.route('/api/kosync-documents', methods=['GET'])
def api_get_kosync_documents():
    """Get all KOSync documents with their link status.
    Optional query param ?user_id= to scope results to a specific user
    (only reachable by admins via _ADMIN_ONLY_ENDPOINTS; when omitted returns all)."""
    user_id = request.args.get('user_id', type=int)
    docs = _database_service.get_all_kosync_documents(user_id=user_id)
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
        # Always update the book's KoSync ID to match the link.
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
                file_path = safe_cache_path(cache_dir, filename)
                if file_path and file_path.exists():
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
