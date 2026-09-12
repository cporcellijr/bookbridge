import glob
import hmac
import html
import logging
import json
import contextvars
import os
import posixpath
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
import schedule
from dependency_injector import providers
from flask import Flask, render_template, render_template_string, request, redirect, url_for, jsonify, session, send_from_directory, make_response, g, current_app, flash
from functools import wraps
from src.utils.user_context import (
    set_current_user_id, reset_current_user_id,
    set_current_user_credentials, reset_current_user_credentials,
    get_current_user_credentials, get_current_user_id,
)
from src.utils.user_config import user_setting
from src.utils.user_config import global_fallback_allowed as _global_fallback_allowed
from src.utils.user_config import SERVICE_ENABLE_KEYS

from src.utils.config_loader import ConfigLoader, KNOWN_SETTING_KEYS, env_truthy
from src.utils.cache_paths import safe_cache_path, safe_library_path, is_plain_basename
from src.utils.ebook_utils import LRUCache
from src.utils.ebook_sources import is_grimmory_source, local_ebook_filename, normalize_ebook_source
from src.utils.logging_utils import memory_log_handler, LOG_PATH
from src.utils.logging_utils import sanitize_log_data
from src.utils.logging_utils import get_persistent_condition_logger
from src.services.diagnostics import setup_diagnostics_logging
from src.api.api_clients import ABS_DISABLED_SENTINEL, is_abs_disabled_value
from src.api.kosync_server import kosync_sync_bp, kosync_admin_bp, init_kosync_server, signal_manifest_rebuild
from src.api.hardcover_routes import hardcover_bp, init_hardcover_routes
from src.api.storygraph_routes import storygraph_bp, init_storygraph_routes
from src.api.bookfusion_upload_client import extract_epub_metadata, _S3_TIMEOUT_LARGE
from src.version import APP_VERSION, get_update_status
from src.db.models import State
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.services.audio_source_adapters import AudioResult, ABSAudioSourceAdapter, BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter
from src.utils.storyteller_transcript import StorytellerTranscript
from src.utils.kosync_headers import kosync_request_kwargs
from src.utils.series_metadata import (
    extract_series_from_abs_metadata as _series_from_abs_metadata,
    extract_series_from_library_detail as _series_from_library_detail,
    extract_series_from_title as _series_from_title,
    resolve_series_details,
    resolve_series_for_book,
)

def _reconfigure_logging():
    """Force update of root logger and all handler levels based on env var."""
    try:
        new_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
        new_level = getattr(logging, new_level_str, logging.INFO)

        root = logging.getLogger()
        root.setLevel(new_level)
        for handler in root.handlers:
            handler.setLevel(new_level)

        logger.info(f"📝 Logging level updated to {new_level_str}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to reconfigure logging: {e}", exc_info=True)

# ---------------- APP SETUP ----------------
container = None
manager = None
database_service = None
SUGGESTIONS_SCAN_JOBS = {}
SUGGESTIONS_SCAN_JOBS_LOCK = threading.Lock()
SUGGESTIONS_SCAN_JOB_TTL_SECONDS = 3600
SUGGESTIONS_STATE_STORE = {}
SUGGESTIONS_STATE_LOCK = threading.Lock()
SUGGESTIONS_STATE_TTL_SECONDS = 86400
SUGGESTIONS_CACHE_FILE_NAME = "suggestions_scan_cache.json"
SUGGESTIONS_CACHE_LOCK = threading.Lock()
# Batch-match queue lives on disk (under DATA_DIR) instead of the Flask session cookie:
# the cookie caps at ~4KB (~a dozen items) and is per-browser; the file store is
# unbounded, survives container rebuilds, and is shared across the admin's tabs.
MATCH_QUEUE_FILE_NAME = "match_queue.json"
MATCH_QUEUE_LOCK = threading.RLock()
STATS_CACHE = {}
STATS_CACHE_LOCK = threading.Lock()
STATS_CACHE_TTL_SECONDS = 60
RESTARTING_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-store, max-age=0">
    <title>Restarting</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0e1623;
            --panel: rgba(12, 23, 38, 0.88);
            --border: rgba(125, 211, 252, 0.2);
            --accent: #7dd3fc;
            --text: #e2e8f0;
            --muted: #94a3b8;
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
            font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top, rgba(14, 165, 233, 0.18), transparent 38%),
                linear-gradient(180deg, #08101a 0%, var(--bg) 100%);
        }

        .panel {
            width: min(520px, 100%);
            padding: 32px 28px;
            border: 1px solid var(--border);
            border-radius: 18px;
            background: var(--panel);
            box-shadow: 0 22px 70px rgba(0, 0, 0, 0.35);
        }

        .status {
            display: inline-flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 18px;
            color: var(--accent);
            font-weight: 600;
            letter-spacing: 0.02em;
        }

        .spinner {
            width: 18px;
            height: 18px;
            border: 2px solid rgba(125, 211, 252, 0.2);
            border-top-color: var(--accent);
            border-radius: 999px;
            animation: spin 0.9s linear infinite;
        }

        h1 {
            margin: 0 0 12px;
            font-size: clamp(1.6rem, 3vw, 2.1rem);
            line-height: 1.15;
        }

        p {
            margin: 0;
            color: var(--muted);
            line-height: 1.6;
        }

        #restart-message {
            margin-top: 18px;
        }

        @keyframes spin {
            to {
                transform: rotate(360deg);
            }
        }
    </style>
</head>
<body>
    <main class="panel">
        <div class="status">
            <span class="spinner" aria-hidden="true"></span>
            <span>Saving settings</span>
        </div>
        <h1>Restarting the application</h1>
        <p>Your settings were saved. This page will send you back to the dashboard as soon as the app is responding again.</p>
        <p id="restart-message">Waiting for the service to come back up...</p>
    </main>

    <script>
        const nextUrl = {{ next_url|tojson }};
        const healthUrl = {{ health_url|tojson }};
        const restartUrl = {{ restart_url|tojson }};
        const statusEl = document.getElementById('restart-message');

        async function beginRestart() {
            statusEl.textContent = 'Requesting restart...';

            try {
                await fetch(restartUrl, {
                    method: 'POST',
                    cache: 'no-store',
                    headers: {
                        'Cache-Control': 'no-store'
                    }
                });
            } catch (error) {
                // The app may already be stopping. Continue polling for readiness.
            }

            statusEl.textContent = 'Restarting application...';
            window.setTimeout(pollUntilReady, 1200);
        }

        async function pollUntilReady() {
            try {
                const response = await fetch(`${healthUrl}?t=${Date.now()}`, {
                    cache: 'no-store',
                    headers: {
                        'Cache-Control': 'no-store'
                    }
                });

                if (response.ok) {
                    statusEl.textContent = 'Application is back. Redirecting...';
                    window.location.replace(nextUrl);
                    return;
                }

                statusEl.textContent = `Still restarting... (${response.status})`;
            } catch (error) {
                statusEl.textContent = 'Still restarting...';
            }

            window.setTimeout(pollUntilReady, 1500);
        }

        window.setTimeout(beginRestart, 100);
    </script>
</body>
</html>
"""

def setup_dependencies(app, test_container=None):
    """
    Initialize dependencies for the web server.

    Args:
        test_container: Optional test container for dependency injection during testing.
                       If None, creates production container from environment.
    """
    global container, manager, database_service, DATA_DIR, EBOOK_DIR, COVERS_DIR

    # Initialize Database Service
    from src.db.migration_utils import initialize_database
    database_service = initialize_database(os.environ.get("DATA_DIR", "/data"))

    # Multi-user: ensure a default admin exists and pre-existing single-user
    # progress/stats are assigned to it (idempotent).
    if database_service:
        from src.db.user_bootstrap import bootstrap_admin_user
        bootstrap_admin_user(database_service)

    # Load settings from DB

    # This updates os.environ with values from the database
    if database_service:
        ConfigLoader.bootstrap_config(database_service)
        # Wrap any credential still stored in plaintext by an older install.
        # Idempotent; runs before settings are mirrored into os.environ.
        try:
            database_service.encrypt_plaintext_secrets()
        except Exception as e:
            logger.error(f"❌ Could not encrypt stored credentials: {e}", exc_info=True)
        ConfigLoader.load_settings(database_service)
        logger.info("✅ Settings loaded into environment variables")

        # One-time upgrade safety net: a service switched on only per-user, with
        # the global left at its seeded 'false', would go dark the moment the
        # install-wide gate became authoritative. Runs once, then never again —
        # so an admin switching a service off later stays off for everyone.
        from src.db.user_bootstrap import reconcile_service_gates
        reconcile_service_gates(database_service)

        # Force reconfigure logging level based on new settings
        _reconfigure_logging()

        # Setup diagnostics warning collector
        setup_diagnostics_logging()

    # RELOAD GLOBALS from updated os.environ

    global LINKER_BOOKS_DIR, STORYTELLER_INGEST, ABS_AUDIO_ROOT
    global STORYTELLER_LIBRARY_DIR, EBOOK_IMPORT_DIR
    global ABS_API_URL, ABS_API_TOKEN, ABS_LIBRARY_ID
    global ABS_COLLECTION_NAME, BOOKLORE_SHELF_NAME, MONITOR_INTERVAL
    global SYNC_PERIOD_MINS, SYNC_DELTA_ABS_SECONDS, SYNC_DELTA_KOSYNC_PERCENT, FUZZY_MATCH_THRESHOLD

    LINKER_BOOKS_DIR = Path(os.environ.get("LINKER_BOOKS_DIR", "/linker_books"))
    STORYTELLER_INGEST = Path(os.environ.get("STORYTELLER_INGEST_DIR", os.environ.get("LINKER_BOOKS_DIR", "/linker_books")))
    ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))
    STORYTELLER_LIBRARY_DIR = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
    EBOOK_IMPORT_DIR = Path(os.environ.get("EBOOK_IMPORT_DIR", "/books"))

    ABS_API_URL = os.environ.get("ABS_SERVER")
    ABS_API_TOKEN = os.environ.get("ABS_KEY")
    ABS_LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID")

    def _get_float_env(key, default):
        try:
            return float(os.environ.get(key, str(default)))
        except (ValueError, TypeError):
            logger.warning(f"⚠️ Invalid '{key}' value, defaulting to {default}", exc_info=True)
            return float(default)

    SYNC_PERIOD_MINS = _get_float_env("SYNC_PERIOD_MINS", 5)
    SYNC_DELTA_ABS_SECONDS = _get_float_env("SYNC_DELTA_ABS_SECONDS", 30)
    SYNC_DELTA_KOSYNC_PERCENT = _get_float_env("SYNC_DELTA_KOSYNC_PERCENT", 0.005)
    FUZZY_MATCH_THRESHOLD = _get_float_env("FUZZY_MATCH_THRESHOLD", 0.8)

    ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")
    BOOKLORE_SHELF_NAME = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")
    MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "3600"))

    logger.info(f"🔄 Globals reloaded from settings (ABS_SERVER={ABS_API_URL})")

    if test_container is not None:
        # Use injected test container
        container = test_container
    else:
        # 3. Create production container AFTER loading settings
        # The container providers (Factories) will now read the updated os.environ values
        from src.utils.di_container import create_container
        container = create_container()

    # 4. Override the container's database_service with our already-initialized instance
    # This ensures consistency and prevents re-initialization
    # Only do this for production containers that support dependency injection
    if test_container is None:
        container.database_service.override(providers.Object(database_service))

    # Initialize manager and services
    manager = container.sync_manager()

    # Wire the SuggestionsService factory into the shelf-watch singleton.
    # web_server.py is the `__main__` entry point; if shelf_watch_service tried
    # `from src.web_server import ...` it would create a second, uninitialized
    # module instance (with container=None), so we inject the factory here.
    try:
        for _sw in container.shelf_watch_services():
            _sw.set_suggestions_service_factory(_get_suggestions_service)
    except Exception as e:
        logger.warning(f"Could not wire shelf_watch_service suggestions factory: {e}", exc_info=True)

    # Get data directories (now using updated env vars)
    DATA_DIR = container.data_dir()
    EBOOK_DIR = container.books_dir()

    # Initialize covers directory
    COVERS_DIR = DATA_DIR / "covers"
    if not COVERS_DIR.exists():
        COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Register KoSync Blueprint and initialize with dependencies
    init_kosync_server(database_service, container, manager, EBOOK_DIR)
    # A catalog change (match added, deleted, or status flipped) invalidates the
    # device-sync manifest, so rebuild it then rather than waiting on the loop.
    #
    # Deliberately NOT hooked to the end of a sync cycle. That was the original
    # invalidation signal, from before this precise one existed, and a cycle only
    # moves reading progress -- which the manifest does not carry. Since instant
    # sync runs a cycle whenever a device or a poller sees movement, and a full
    # rebuild of a few hundred books takes minutes, that proxy signal kept the
    # prebuilder rebuilding an identical manifest back to back all day.
    database_service.register_catalog_change_callback(signal_manifest_rebuild)
    app.register_blueprint(kosync_sync_bp)
    app.register_blueprint(kosync_admin_bp)

    # Register Hardcover Blueprint and initialize with dependencies
    init_hardcover_routes(database_service, container)
    app.register_blueprint(hardcover_bp)
    init_storygraph_routes(database_service, container)
    app.register_blueprint(storygraph_bp)

    logger.info(f"🚀 Web server dependencies initialized (DATA_DIR={DATA_DIR})")







# Audiobook files location
ABS_AUDIO_ROOT = Path(os.environ.get("AUDIOBOOKS_DIR", "/audiobooks"))

# ABS API Configuration
ABS_API_URL = os.environ.get("ABS_SERVER")
ABS_API_TOKEN = os.environ.get("ABS_KEY")
ABS_LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID")

# ABS Collection name for auto-adding matched books
ABS_COLLECTION_NAME = os.environ.get("ABS_COLLECTION_NAME", "Synced with KOReader")

# Grimmory shelf name for auto-adding matched books
BOOKLORE_SHELF_NAME = os.environ.get("BOOKLORE_SHELF_NAME", "Kobo")




# Storyteller Forge
STORYTELLER_LIBRARY_DIR = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))

# Track active forge operations for UI status
# Track active forge operations for UI status - MOVED TO FORGE SERVICE


# ---------------- HELPER FUNCTIONS ----------------
def get_audiobooks_conditionally():
    """Get audiobooks either from specific library or all libraries based on ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID setting."""
    from src.utils.user_config import user_setting
    raw_scope = (user_setting("ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID") or "").strip()
    abs_library_id = None
    lowered = raw_scope.lower()
    if lowered in {"true", "1", "yes", "on"}:
        abs_library_id = (user_setting("ABS_LIBRARY_ID") or "").strip() or None
    elif lowered in {"false", "0", "no", "off", "none", ""}:
        abs_library_id = None
    else:
        # Backward-compatible mode where this env var directly contains the library id.
        abs_library_id = raw_scope

    abs_client = uc().abs_client
    if abs_library_id:
        # Fetch audiobooks only from the specified library
        return abs_client.get_audiobooks_for_lib(abs_library_id)
    else:
        # Fetch all audiobooks from all libraries
        return abs_client.get_all_audiobooks()


def _normalize_abs_form_value(key: str, raw_value) -> str:
    clean_value = str(raw_value or "").strip()
    if not clean_value:
        return ""
    if key in {"ABS_SERVER", "ABS_KEY"} and is_abs_disabled_value(clean_value):
        return ABS_DISABLED_SENTINEL
    if key == "ABS_SERVER" and not clean_value.lower().startswith(("http://", "https://")):
        return f"http://{clean_value}"
    return clean_value


def _display_abs_server() -> str:
    abs_server = os.environ.get("ABS_SERVER", "")
    if is_abs_disabled_value(abs_server):
        return ""
    return abs_server

# ---------------- AUTH (multi-user) ----------------
# Endpoints reachable without a web session. The device-facing KoSync sync
# blueprint ('kosync') carries its own per-device auth and is exempted by
# blueprint name in the guard below. The two kosync_admin plugin routes serve
# only the static BridgeSync plugin zip + version (no user data) and are public
# so the settings-page link and KOReader self-update can fetch them directly.
_AUTH_EXEMPT_ENDPOINTS = {
    'login', 'logout', 'setup', 'api_health', 'static',
    'kosync_admin.admin_plugin_version',
    'kosync_admin.admin_plugin_download',
}
_LOGIN_FAILURES: dict[tuple[str, str], list[float]] = {}
_LOGIN_FAILURES_LOCK = threading.Lock()
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURE_WINDOW = 60

# Endpoints only admins may reach. Regular users get a simple home + match /
# forge / sync; global engine config, library-wide tools, logs, stats and
# suggestions are admin-only ("admin sets things up, users just use").
_ADMIN_ONLY_ENDPOINTS = {
    'settings',
    'suggestions', 'get_suggestions', 'suggestions_scan_status',
    'dismiss_suggestion', 'ignore_suggestion', 'clear_stale_suggestions',
    'stats_view', 'api_stats', 'api_stats_reading_day', 'api_stats_reading_calendar',
    'api_stats_book_detail', 'api_stats_yearly_recap',
    'logs_view', 'api_logs', 'api_logs_live', 'view_log',
    'clean_cache', 'api_series_backfill', 'api_debug_abs_series', 'api_storyteller_backfill',
    'admin_users', 'admin_user_integrations',
    'api_restart', 'test_connection',
    'get_booklore_libraries', 'get_booklore_shelves', 'get_abs_libraries',
    'api_booklore_refresh', 'alignments_llm_status', 'alignments_realign', 'alignments_restore',
    'kosync_admin.api_get_kosync_documents',
    'kosync_admin.api_link_kosync_document',
    'kosync_admin.api_unlink_kosync_document',
    'kosync_admin.api_delete_kosync_document',
    'my_reports',
    'api_diagnostics_submissions',
}


def current_user():
    """Return the logged-in User for this request (cached on g), or None."""
    if 'current_user' in g.__dict__:
        return g.current_user
    user = None
    uid = session.get('user_id')
    if uid and database_service is not None:
        try:
            candidate = database_service.get_user(uid)
            if candidate and candidate.active:
                user = candidate
        except Exception:
            user = None
    g.current_user = user
    return user


class _UnavailableClient:
    def is_configured(self):
        return False


_UNAVAILABLE_CLIENT = _UnavailableClient()


class _GlobalClients:
    """Fallback that exposes the global singletons under the same attribute names
    as a per-user bundle (used for unauthenticated/admin/global contexts)."""
    @property
    def abs_client(self): return container.abs_client()
    @property
    def booklore_client(self): return container.booklore_client()
    @property
    def bookfusion_client(self): return container.bookfusion_client()
    @property
    def bookorbit_client(self): return container.bookorbit_client()
    @property
    def kavita_client(self):
        provider = getattr(container, "kavita_client", None)
        return provider() if provider else _UNAVAILABLE_CLIENT
    @property
    def cwa_client(self): return container.cwa_client()
    @property
    def storyteller_client(self): return container.storyteller_client()
    @property
    def hardcover_client(self): return container.hardcover_client()
    @property
    def storygraph_client(self): return container.storygraph_client()
    @property
    def library_service(self): return container.library_service()
    @property
    def sync_clients(self): return container.sync_clients()


_global_clients = _GlobalClients()


# Lets background work (e.g. batch-match processing) re-bind the request's client
# bundle onto its own thread, so uc()-internal helpers resolve the right user's
# clients instead of silently falling back to the global bundle (current_user()
# reads the Flask request context, which a worker thread doesn't have).
_active_bundle: "contextvars.ContextVar" = contextvars.ContextVar("active_client_bundle", default=None)

# When True, _spawn_user_background runs inline instead of on a daemon thread. Set by
# create_app() under a test container so integration tests stay deterministic.
_BACKGROUND_TASKS_SYNCHRONOUS = False


def uc():
    """The active client bundle for this request: the logged-in user's own
    clients (per-user credentials/library) when available, else the global
    singletons. Use this in user-facing flows (match/forge/search) so they act
    on the user's library, not the admin's."""
    override = _active_bundle.get()
    if override is not None:
        return override
    try:
        user = current_user()
    except RuntimeError:
        user = None
    if user is None:
        # Background per-user work has no Flask request context. The sync
        # cycle/poller binds the ambient user id before invoking helpers such
        # as SuggestionsService, so resolve that user's bundle instead of
        # silently falling back to the admin/global clients.
        user_id = get_current_user_id()
        if user_id is not None:
            try:
                return container.user_client_registry().get_clients(user_id)
            except Exception as e:
                logger.debug("uc(): ambient user bundle unavailable: %s", e)
    if user is not None:
        try:
            return container.user_client_registry().get_clients(user.id)
        except Exception as e:
            logger.debug("uc(): falling back to global clients: %s", e)
    return _global_clients


def _client_bundle_kwargs(clients):
    """Thread a real per-user bundle into background work; omit global fallback."""
    if isinstance(clients, _GlobalClients):
        return {}
    return {"client_bundle": clients}


def _optional_client_configured(clients, attribute: str) -> bool:
    """Safely test an optional bundle client, including legacy test bundles."""
    if attribute not in getattr(clients, "__dict__", {}) and not hasattr(type(clients), attribute):
        return False
    client = getattr(clients, attribute, None)
    try:
        return bool(client and client.is_configured())
    except Exception:
        return False


# --- Deferred tracker auto-match -------------------------------------------------
# Hardcover/StoryGraph auto-match downloads the EPUB, scrapes/queries the tracker,
# and (when OLLAMA_TRACKER_MATCH is on) calls the local Ollama judge — easily tens
# of seconds, and N× that for a batch. None of it affects what the dashboard shows,
# so we run it off the request thread and redirect immediately. A single worker
# drains the queue serially on purpose: auto-match hits Ollama with
# MAX_LOADED_MODELS=1, so concurrent jobs would only thrash the model.
_TRACKER_AUTOMATCH_QUEUE: "queue.Queue" = queue.Queue()
_tracker_automatch_worker_started = False
_tracker_automatch_worker_lock = threading.Lock()


def _tracker_automatch_worker():
    while True:
        sync_clients, book = _TRACKER_AUTOMATCH_QUEUE.get()
        try:
            abs_id = getattr(book, "abs_id", "?")
            hardcover = sync_clients.get("Hardcover")
            if hardcover and hardcover.is_configured():
                try:
                    hardcover._automatch_hardcover(book)
                except Exception as e:
                    logger.warning("Deferred Hardcover automatch failed for '%s': %s", abs_id, e, exc_info=True)
            storygraph = sync_clients.get("StoryGraph")
            if storygraph and storygraph.is_configured():
                try:
                    storygraph._automatch_storygraph(book)
                except Exception as e:
                    logger.warning("Deferred StoryGraph automatch failed for '%s': %s", abs_id, e, exc_info=True)
        except Exception as e:
            logger.error("Deferred tracker automatch worker error: %s", e, exc_info=True)
        finally:
            _TRACKER_AUTOMATCH_QUEUE.task_done()


def _enqueue_tracker_automatch(sync_clients, book):
    """Queue Hardcover/StoryGraph auto-match for `book` to run after the response.

    `sync_clients` is the active bundle's sync-client dict, already resolved for the
    request's user; it's copied and captured so the worker never touches request
    context (where `uc()` would silently fall back to the global bundle). Idempotent
    downstream: each client early-returns if the book is already linked.
    """
    if not book:
        return
    try:
        snapshot = dict(sync_clients) if sync_clients else {}
    except Exception:
        snapshot = {}
    if not snapshot:
        return
    global _tracker_automatch_worker_started
    with _tracker_automatch_worker_lock:
        if not _tracker_automatch_worker_started:
            threading.Thread(target=_tracker_automatch_worker, daemon=True, name="tracker-automatch").start()
            _tracker_automatch_worker_started = True
    _TRACKER_AUTOMATCH_QUEUE.put((snapshot, book))


def _spawn_user_background(fn, *args, label="background"):
    """Run `fn(*args)` on a daemon thread with the request's user context re-bound.

    Batch-match processing (artifact downloads, hash, transcript ingest, save,
    collection/shelf) is slow and N×, but none of it needs the response. We capture
    the active bundle + ambient user id/credentials in the request and re-bind them in
    the worker so `uc()` / `user_setting` / DB ownership resolve for the same user
    off-thread (a worker thread has no Flask request context). Errors are logged.

    In test mode (`create_app(test_container=...)`) it runs inline so integration
    tests observe the side effects right after the request — the request context is
    still active there, so `uc()` resolves normally without the re-bind.
    """
    if _BACKGROUND_TASKS_SYNCHRONOUS:
        try:
            fn(*args)
        except Exception as e:
            logger.error("%s failed: %s", label, e, exc_info=True)
        return

    bundle = uc()
    try:
        user = current_user()
        user_id = user.id if user is not None else None
    except Exception:
        user_id = None
    creds = get_current_user_credentials()

    def runner():
        tok_bundle = _active_bundle.set(bundle)
        tok_uid = set_current_user_id(user_id)
        tok_creds = set_current_user_credentials(creds)
        try:
            fn(*args)
        except Exception as e:
            logger.error("%s failed: %s", label, e, exc_info=True)
        finally:
            reset_current_user_credentials(tok_creds)
            reset_current_user_id(tok_uid)
            _active_bundle.reset(tok_bundle)

    threading.Thread(target=runner, daemon=True, name=label).start()


def _request_wants_json() -> bool:
    """True for API/XHR requests that should get a 401 instead of a redirect."""
    if request.path.startswith('/api/'):
        return True
    accept = request.headers.get('Accept', '')
    if 'application/json' in accept and 'text/html' not in accept:
        return True
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def require_login_guard():
    """before_request hook: require a web session for everything except the
    auth/health endpoints, static files, and the device KoSync sync blueprint."""
    if current_app.config.get('LOGIN_DISABLED'):
        return None
    endpoint = request.endpoint
    if endpoint is None:
        return None
    setup_required = False
    if database_service is not None:
        try:
            setup_required = database_service.count_users() == 0
        except Exception:
            setup_required = False
    if setup_required and endpoint != 'setup':
        if _request_wants_json():
            return jsonify({"error": "initial admin setup required"}), 503
        return redirect(url_for('setup', next=request.full_path if request.query_string else request.path))
    if endpoint in _AUTH_EXEMPT_ENDPOINTS:
        return None
    if request.blueprint == 'kosync':  # device sync API — own auth
        return None
    user = current_user()
    if user is None:
        remote_user = _remote_auth_user()
        if remote_user is not None:
            session['user_id'] = remote_user.id
            session['username'] = remote_user.username
            session['role'] = remote_user.role
            session.permanent = True
            g.current_user = remote_user
            user = remote_user
            try:
                database_service.touch_user_login(remote_user.id)
            except Exception:
                pass
    if user is None:
        if _request_wants_json():
            return jsonify({"error": "authentication required"}), 401
        return redirect(url_for('login', next=request.full_path if request.query_string else request.path))
    # Scope this request to the user: their per-user settings (library id, enable
    # flags, search scope) and clients. Reset in teardown (threads are reused).
    _bind_request_user_context(user)
    # Logged in — enforce admin-only areas.
    if endpoint in _ADMIN_ONLY_ENDPOINTS and not user.is_admin:
        if _request_wants_json():
            return jsonify({"error": "admin access required"}), 403
        return ("Forbidden: admin access required", 403)
    return None


_CSRF_SESSION_KEY = '_csrf_token'
_CSRF_HEADER = 'X-CSRF-Token'
_CSRF_FORM_FIELD = 'csrf_token'
_CSRF_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}

# Injected into authenticated HTML pages so the existing templates need no
# per-form changes: forwards the per-session CSRF token on same-origin fetch()
# calls, form submits (including programmatic .submit()), and requestSubmit().
_CSRF_BOOTSTRAP_TEMPLATE = """<script>(function(){
  var t = "__CSRF_TOKEN__";
  var safe = {GET:1, HEAD:1, OPTIONS:1, TRACE:1};
  function sameOrigin(url){
    try { return new URL(url, window.location.href).origin === window.location.origin; }
    catch(e){ return true; }
  }
  var _fetch = window.fetch;
  if (_fetch) {
    window.fetch = function(input, init){
      init = init || {};
      var method = (init.method || (input && input.method) || 'GET').toUpperCase();
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      if (!safe[method] && sameOrigin(url)) {
        var src = init.headers || (typeof input !== 'string' && input ? input.headers : undefined) || {};
        var h = new Headers(src);
        if (!h.has('X-CSRF-Token')) h.set('X-CSRF-Token', t);
        init.headers = h;
      }
      return _fetch.call(this, input, init);
    };
  }
  function addField(form){
    try {
      var method = (form.getAttribute('method') || 'GET').toUpperCase();
      if (safe[method]) return;
      if (form.action && !sameOrigin(form.action)) return;
      if (form.querySelector('input[name="csrf_token"]')) return;
      var inp = document.createElement('input');
      inp.type = 'hidden'; inp.name = 'csrf_token'; inp.value = t;
      form.appendChild(inp);
    } catch(e){}
  }
  document.addEventListener('submit', function(ev){
    var form = ev.target;
    if (form && form.tagName === 'FORM') addField(form);
  }, true);
  var _submit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function(){ addField(this); return _submit.apply(this, arguments); };
})();</script>"""


def _ensure_csrf_token() -> str:
    """Return the per-session CSRF token, generating one on first use."""
    token = session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


def csrf_protect_guard():
    """before_request hook: reject cross-site state-changing requests that are
    authenticated by the browser session cookie.

    Only session-authenticated mutations are checked. API clients that
    authenticate by a header key (KoSync devices, Hardcover) never carry a web
    session, so they are naturally exempt — no per-route allowlist needed.
    Disabled under the test harness (CSRF_ENABLED is set False alongside
    LOGIN_DISABLED in create_app)."""
    if not current_app.config.get('CSRF_ENABLED', True):
        return None
    if request.method in _CSRF_SAFE_METHODS:
        return None
    # Only browser-session requests are CSRF-eligible. No session user -> the
    # request is either pre-login (login/setup) or header-authenticated (device
    # APIs); neither relies on the ambient session cookie, so skip.
    if session.get('user_id') is None:
        return None
    expected = session.get(_CSRF_SESSION_KEY)
    submitted = request.headers.get(_CSRF_HEADER) or request.form.get(_CSRF_FORM_FIELD)
    if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
        logger.warning(
            f"⚠️ CSRF: rejected {request.method} {request.path} from "
            f"'{request.remote_addr}' (user {session.get('user_id')})"
        )
        if _request_wants_json():
            return jsonify({"error": "CSRF token missing or invalid"}), 403
        return ("CSRF token missing or invalid", 403)
    return None


def inject_csrf_script(response):
    """after_request hook: embed the CSRF bootstrap into authenticated HTML
    pages so fetch()/form submits forward the per-session token automatically."""
    try:
        if not current_app.config.get('CSRF_ENABLED', True):
            return response
        if session.get('user_id') is None:
            return response
        if response.status_code != 200 or response.mimetype != 'text/html':
            return response
        if response.direct_passthrough:
            return response
        body = response.get_data(as_text=True)
        idx = body.rfind('</body>')
        if idx == -1:
            return response
        snippet = _CSRF_BOOTSTRAP_TEMPLATE.replace('__CSRF_TOKEN__', _ensure_csrf_token())
        response.set_data(body[:idx] + snippet + body[idx:])
    except Exception as e:  # never let CSRF wiring break a page render
        logger.debug(f"CSRF inject skipped: {e}")
    return response


def _bind_request_user_context(user):
    """Set the ambient per-user id + credentials for this request (reset in
    teardown). Tokens are stashed on g."""
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY
    try:
        creds = database_service.get_user_credentials(user.id) if database_service else {}
    except Exception:
        creds = {}
    # Mark whether global (admin) env fallback is permitted. Without this flag,
    # resolve_setting's `... is False` guard reads None on a non-admin's creds
    # and silently falls back to the admin's global values (per-user leak). Only
    # the primary admin inherits the global config — it is their own account
    # mirrored outward — so every other user, second admins included, is
    # isolated. This ambient dict is also what _spawn_user_background copies
    # onto worker threads.
    creds = dict(creds)
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = _global_fallback_allowed(database_service, user)
    g._uctx_id_token = set_current_user_id(user.id)
    g._uctx_creds_token = set_current_user_credentials(creds)


def _release_request_user_context(_exc=None):
    tok = g.pop('_uctx_id_token', None) if hasattr(g, 'pop') else None
    if tok is not None:
        reset_current_user_id(tok)
    tok = g.pop('_uctx_creds_token', None) if hasattr(g, 'pop') else None
    if tok is not None:
        reset_current_user_credentials(tok)


def admin_required(f):
    """Decorator for routes that require an admin user (e.g. user management)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if current_app.config.get('LOGIN_DISABLED'):
            return f(*args, **kwargs)
        user = current_user()
        if user is None:
            if _request_wants_json():
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for('login', next=request.path))
        if not user.is_admin:
            return ("Forbidden: admin access required", 403)
        return f(*args, **kwargs)
    return wrapper


def _safe_next_url(default=None):
    """Resolve the post-auth redirect target, allowing only same-site relative
    paths. Absolute (`http://…`) and protocol-relative (`//host`, `/\\host`)
    values are rejected to prevent an open redirect."""
    fallback = default or url_for('index')
    next_url = request.args.get('next') or fallback
    if (
        not next_url.startswith('/')
        or next_url.startswith('//')
        or next_url.startswith('/\\')
    ):
        return fallback
    return next_url


_LOOPBACK_PROXY_NETWORKS = ('127.0.0.0/8', '::1/128')


def _trusted_proxy_networks() -> list:
    """Networks allowed to present the remote-auth header, read per call.

    Empty/unset means loopback only. Parsed per call so the Settings UI applies
    without a restart; malformed entries are dropped rather than silently
    widening (or emptying) the trust list.
    """
    import ipaddress

    raw = os.environ.get('REMOTE_AUTH_TRUSTED_PROXIES', '') or ''
    entries = [part.strip() for part in raw.replace('\n', ',').split(',') if part.strip()]
    if not entries:
        entries = list(_LOOPBACK_PROXY_NETWORKS)

    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            get_persistent_condition_logger().warn(
                logger,
                f"remote_auth_bad_proxy_entry:{entry}",
                "⚠️ REMOTE_AUTH_TRUSTED_PROXIES entry %s is not a valid IP or CIDR — ignoring it",
                sanitize_log_data(entry),
            )
    return networks


def _remote_auth_peer_trusted() -> bool:
    """Whether this request's direct peer may present the remote-auth header.

    The header is a complete authentication bypass, so it is only honoured from a
    configured proxy address. Without this check anything able to reach the
    published port could send `Remote-User: admin`. `request.remote_addr` is the
    real TCP peer here (the app installs no ProxyFix/X-Forwarded-For rewriting),
    so it cannot be spoofed by a client header.
    """
    import ipaddress

    peer = (request.remote_addr or '').strip()
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in _trusted_proxy_networks())


def _remote_auth_user():
    """Return the User named by the configured reverse-proxy header, or None."""
    if not env_truthy('REMOTE_AUTH_ENABLED'):
        return None
    header_name = os.environ.get('REMOTE_AUTH_HEADER', 'Remote-User').strip() or 'Remote-User'
    username = (request.headers.get(header_name) or '').strip()
    if not username or database_service is None:
        return None
    if not _remote_auth_peer_trusted():
        # Someone reached the port directly and supplied the header. That is either
        # a misconfigured trust list or an attempt to bypass login, and both need to
        # be visible — but it can repeat on every request, so it is rate-limited.
        get_persistent_condition_logger().warn(
            logger,
            f"remote_auth_untrusted_peer:{request.remote_addr}",
            "🔒 Rejected %s header from untrusted address %s — add the reverse proxy "
            "to REMOTE_AUTH_TRUSTED_PROXIES if this is your proxy (empty = loopback only)",
            sanitize_log_data(header_name),
            sanitize_log_data(request.remote_addr or 'unknown'),
        )
        return None
    try:
        user = database_service.get_user_by_username(username)
    except Exception:
        return None
    if user is None or not user.active:
        return None
    return user


def _establish_session_and_redirect(user):
    """Set up an authenticated session for `user` and redirect to a safe next."""
    session['user_id'] = user.id
    session['username'] = user.username
    session['role'] = user.role
    session.permanent = True
    try:
        database_service.touch_user_login(user.id)
    except Exception:
        pass
    return redirect(_safe_next_url())


def login():
    """Render/handle the login form."""
    if database_service is not None:
        try:
            if database_service.count_users() == 0:
                return redirect(url_for('setup', next=request.args.get('next') or url_for('index')))
        except Exception:
            pass
    if current_user() is not None:
        return redirect(url_for('index'))

    remote_user = _remote_auth_user()
    if remote_user is not None:
        return _establish_session_and_redirect(remote_user)

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        failure_key = (username.lower(), request.remote_addr or "")
        now = time.time()
        with _LOGIN_FAILURES_LOCK:
            failures = [t for t in _LOGIN_FAILURES.get(failure_key, []) if now - t < _LOGIN_FAILURE_WINDOW]
            if failures:
                _LOGIN_FAILURES[failure_key] = failures
            else:
                _LOGIN_FAILURES.pop(failure_key, None)
        if len(failures) >= _LOGIN_FAILURE_LIMIT:
            return render_template('login.html', error="Too many login attempts. Try again shortly."), 429
        user = None
        if database_service is not None:
            user = database_service.verify_user_credentials(username, password)
        if user:
            with _LOGIN_FAILURES_LOCK:
                _LOGIN_FAILURES.pop(failure_key, None)
            return _establish_session_and_redirect(user)
        with _LOGIN_FAILURES_LOCK:
            _LOGIN_FAILURES[failure_key] = failures + [now]
        error = "Invalid username or password"
        logger.warning("Failed login attempt for username '%s' from %s", username, request.remote_addr)

    return render_template('login.html', error=error), (401 if error else 200)


def setup():
    """First-run admin setup. Only available while no users exist."""
    if database_service is None:
        return ("Database not initialized", 500)
    try:
        if database_service.count_users() != 0:
            return redirect(url_for('login'))
    except Exception as e:
        logger.error("Could not inspect users for setup: %s", e, exc_info=True)
        return ("Database unavailable", 500)

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not username:
            error = "Username is required"
        elif not password:
            error = "Password is required"
        elif password != confirm_password:
            error = "Passwords do not match"
        else:
            try:
                from src.db.user_bootstrap import create_initial_admin_user
                user, _counts = create_initial_admin_user(database_service, username, password)
                session.clear()
                return _establish_session_and_redirect(user)
            except ValueError:
                return redirect(url_for('login'))
            except Exception as e:
                logger.error("Initial admin setup failed: %s", e, exc_info=True)
                error = "Could not create admin account"

    return render_template('setup.html', error=error), (400 if error else 200)


def logout():
    session.clear()
    return redirect(url_for('login'))


def account():
    """Self-service account page: change own username and/or password.

    Requires the current password to make any change. Admin management of other
    users is a separate (Phase 6) admin UI.
    """
    user = current_user()
    if user is None:
        return redirect(url_for('login'))

    error = None
    message = None
    if request.method == 'POST':
        current_password = request.form.get('current_password') or ''
        new_username = (request.form.get('username') or '').strip()
        new_password = request.form.get('new_password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not database_service.verify_user_credentials(user.username, current_password):
            error = "Current password is incorrect"
        elif new_password and new_password != confirm_password:
            error = "New passwords do not match"
        else:
            changed = []
            if new_username and new_username != user.username:
                ok, err = database_service.set_username(user.id, new_username)
                if not ok:
                    error = err
                else:
                    session['username'] = new_username
                    changed.append("username")
            if not error and new_password:
                database_service.set_user_password(user.id, new_password)
                changed.append("password")
            if not error:
                message = "Updated " + " and ".join(changed) + "." if changed else "No changes made."
                user = database_service.get_user(user.id)  # refresh for display

    return render_template(
        'account.html',
        error=error,
        message=message,
        account_user=user,
    )


def account_integrations():
    """Self-service integration settings for the signed-in user."""
    user = current_user()
    if user is None:
        return redirect(url_for('login'))

    from src.utils.user_config import PER_USER_FIELD_GROUPS

    message = None
    if request.method == 'POST':
        _apply_user_integrations(user.id)
        message = "Saved your integrations."
        user = database_service.get_user(user.id)

    creds = database_service.get_user_credentials(user.id)
    master = {
        key: os.environ.get(key, '')
        for _g, fields in PER_USER_FIELD_GROUPS for key, _l, _t in fields
    }
    return render_template(
        'account_integrations.html',
        groups=PER_USER_FIELD_GROUPS,
        creds=creds,
        master=master,
        service_enable_keys=SERVICE_ENABLE_KEYS,
        allow_master_fallback=_global_fallback_allowed(database_service, user),
        message=message,
        account_user=user,
        user_test_services=_USER_TEST_SERVICES,
    )


# Library-lookup credentials the primary admin's account also lends to the
# engine's global singletons (shelf-watch, scans, suggestions, ABS socket,
# manifest). Mirrored to the global settings when the primary admin saves.
from src.utils.user_config import ENGINE_MIRROR_KEYS as _ENGINE_MIRROR_KEYS


def _apply_user_integrations(user_id):
    """Save the posted per-user integration fields to a user's credential store
    and invalidate their cached client bundle. Secrets keep-if-blank, text
    clears-if-blank, toggles save true/false.

    Admin users can inherit master/global settings; regular users require
    explicit per-user account values so they do not accidentally sync admin
    libraries.
    """
    from src.utils.user_config import PER_USER_FIELD_GROUPS
    for _group, fields in PER_USER_FIELD_GROUPS:
        for key, _label, ftype in fields:
            if ftype == 'bool':
                database_service.set_user_credential(user_id, key, 'true' if key in request.form else 'false')
            elif ftype == 'secret':
                submitted = request.form.get(key, '')
                if submitted:  # blank => keep existing secret
                    database_service.set_user_credential(user_id, key, submitted)
            else:  # text/select: blank clears => inherit master/default
                database_service.set_user_credential(user_id, key, request.form.get(key, ''))

    # A newly-entered Readest password belongs to a (possibly different) account,
    # so any cached Supabase tokens are stale — drop them so the next sync logs in
    # fresh with the new credentials rather than refreshing the old session.
    if request.form.get('READEST_PASSWORD'):
        for _tok in ('READEST_ACCESS_TOKEN', 'READEST_REFRESH_TOKEN', 'READEST_TOKEN_EXPIRES_AT'):
            database_service.set_user_credential(user_id, _tok, '')

    try:
        container.user_client_registry().invalidate(user_id)
    except Exception as e:
        logger.debug("Could not invalidate client bundle for user %s: %s", user_id, e)

    # Keep the engine's global config in sync with the primary admin's account.
    # The global singletons (shelf-watch, library scans, suggestions, the global
    # ABS socket, manifest) authenticate with the primary admin, so mirror that
    # admin's library-lookup creds to the global settings. Background features
    # pick these up on the next restart; the admin's own per-user bundle was
    # rebuilt by the invalidate above.
    try:
        primary_admin_id = database_service._default_user_id()
    except Exception:
        primary_admin_id = None
    if primary_admin_id is not None and user_id == primary_admin_id:
        for key in _ENGINE_MIRROR_KEYS:
            val = database_service.get_user_credential(user_id, key)
            # Blank optional library IDs deliberately remove the global filter;
            # other blanks keep the configured value/default they inherit.
            if val or (val == '' and key in {'ABS_LIBRARY_ID', 'BOOKLORE_LIBRARY_ID'}):
                database_service.set_setting(key, val)
                os.environ[key] = val


@admin_required
def admin_user_integrations(user_id):
    """Admin-managed per-user integrations. The admin sets each user's
    credentials/library here; the admin's own integrations are the master
    Settings. Regular users do not inherit blank account credentials."""
    target = database_service.get_user(user_id)
    if not target:
        return ("User not found", 404)

    from src.utils.user_config import PER_USER_FIELD_GROUPS

    message = None
    if request.method == 'POST':
        _apply_user_integrations(user_id)
        message = f"Saved integrations for {target.username}."
        target = database_service.get_user(user_id)

    creds = database_service.get_user_credentials(user_id)
    # Master/global value per key, shown as the inherited fallback hint.
    master = {
        key: os.environ.get(key, '')
        for _g, fields in PER_USER_FIELD_GROUPS for key, _l, _t in fields
    }
    return render_template(
        'admin_user_integrations.html',
        groups=PER_USER_FIELD_GROUPS,
        creds=creds,
        master=master,
        service_enable_keys=SERVICE_ENABLE_KEYS,
        allow_master_fallback=_global_fallback_allowed(database_service, target),
        message=message,
        target_user=target,
        user_test_services=_USER_TEST_SERVICES,
    )


_USER_TEST_SERVICES = {
    "Audiobookshelf": "abs",
    "KOReader / KoSync": "kosync",
    "Storyteller": "storyteller",
    "Grimmory": "booklore",
    "BookFusion": "bookfusion",
    "BookOrbit": "bookorbit",
    "Kavita": "kavita",
    "Readest": "readest",
    "Calibre-Web Automated": "cwa",
    "Hardcover": "hardcover",
    "StoryGraph": "storygraph",
}


_TEST_CONNECTION_FIELDS = {
    'abs': ['ABS_SERVER', 'ABS_KEY'],
    'kosync': [
        'KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY',
        'KOSYNC_AUTH_METHOD',
    ],
    'storyteller': ['STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD'],
    'booklore': ['BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD'],
    'bookorbit': ['BOOKORBIT_ENABLED', 'BOOKORBIT_SERVER', 'BOOKORBIT_USER', 'BOOKORBIT_PASSWORD'],
    'kavita': ['KAVITA_ENABLED', 'KAVITA_SERVER', 'KAVITA_API_KEY'],
    'bookfusion': ['BOOKFUSION_ENABLED', 'BOOKFUSION_API_URL', 'BOOKFUSION_ACCESS_TOKEN'],
    'cwa': ['CWA_ENABLED', 'CWA_SERVER', 'CWA_USERNAME', 'CWA_PASSWORD', 'CWA_SYNC_TOKEN'],
    'readest': ['READEST_ANNOTATION_SYNC', 'READEST_EMAIL', 'READEST_PASSWORD', 'READEST_SUPABASE_URL'],
    'hardcover': ['HARDCOVER_ENABLED', 'HARDCOVER_TOKEN'],
    'storygraph': ['STORYGRAPH_ENABLED', 'STORYGRAPH_SESSION_COOKIE', 'STORYGRAPH_REMEMBER_USER_TOKEN'],
}


def _posted_user_test_credentials(target, submitted):
    """Resolve saved per-user credentials plus unsaved form edits for a test.

    Secret blanks keep the stored value, matching the save form. Regular users
    do not inherit global account credentials; admin users may.
    """
    from src.utils.user_config import (
        PER_USER_CREDENTIAL_KEYS,
        PER_USER_FIELD_GROUPS,
        _ALLOW_GLOBAL_FALLBACK_KEY,
        resolve_setting,
    )

    stored = database_service.get_user_credentials(target.id) or {}
    creds = {k: v for k, v in stored.items() if k in PER_USER_CREDENTIAL_KEYS}
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = _global_fallback_allowed(database_service, target)

    field_types = {
        key: ftype
        for _group, fields in PER_USER_FIELD_GROUPS
        for key, _label, ftype in fields
    }
    for key, ftype in field_types.items():
        if key not in submitted:
            continue
        if ftype == 'secret' and not submitted.get(key):
            continue
        creds[key] = submitted.get(key)

    payload = {}
    for service_fields in _TEST_CONNECTION_FIELDS.values():
        for key in service_fields:
            if key in PER_USER_CREDENTIAL_KEYS:
                # A connection test validates credentials, so it is not subject to
                # the install-wide service gate: an admin must be able to check a
                # user's account before switching the service on for everyone.
                payload[key] = resolve_setting(creds, key, "", enforce_global_gate=False)
            else:
                payload[key] = os.environ.get(key, "")
    return payload


@admin_required
def admin_user_test_connection(user_id, service):
    target = database_service.get_user(user_id)
    if not target:
        return jsonify({"ok": False, "message": "User not found"}), 404
    payload = _posted_user_test_credentials(target, request.get_json(silent=True) or {})
    return _run_test_connection(service, payload)


def account_test_connection(service):
    user = current_user()
    if user is None:
        return jsonify({"ok": False, "message": "Authentication required"}), 401
    payload = _posted_user_test_credentials(user, request.get_json(silent=True) or {})
    return _run_test_connection(service, payload)


@admin_required
def admin_user_abs_libraries(user_id):
    """List this user's Audiobookshelf libraries, using the credentials posted
    from the integrations form (so the admin can look up before saving)."""
    target = database_service.get_user(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    payload = _posted_user_test_credentials(target, request.get_json(silent=True) or {})
    from src.api.api_clients import ABSClient
    client = ABSClient(credentials=payload)
    if not client.is_configured():
        return jsonify({"error": "Audiobookshelf not configured for this user (set the API token above)"}), 400
    try:
        return jsonify(client.get_libraries() or [])
    except Exception as e:
        logger.warning("Per-user ABS library lookup failed for user %s: %s", user_id, e, exc_info=True)
        return jsonify({"error": str(e)}), 502


def account_abs_libraries():
    """List current user's Audiobookshelf libraries from unsaved form credentials."""
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required"}), 401
    payload = _posted_user_test_credentials(user, request.get_json(silent=True) or {})
    from src.api.api_clients import ABSClient
    client = ABSClient(credentials=payload)
    if not client.is_configured():
        return jsonify({"error": "Audiobookshelf not configured for this user (set the API token above)"}), 400
    try:
        return jsonify(client.get_libraries() or [])
    except Exception as e:
        logger.warning("Self-service ABS library lookup failed for user %s: %s", user.id, e, exc_info=True)
        return jsonify({"error": str(e)}), 502


@admin_required
def admin_user_booklore_libraries(user_id):
    """List this user's Grimmory libraries, using the credentials posted from
    the integrations form."""
    target = database_service.get_user(user_id)
    if not target:
        return jsonify({"error": "User not found"}), 404
    payload = _posted_user_test_credentials(target, request.get_json(silent=True) or {})
    from src.api.booklore_client import BookloreClient
    client = BookloreClient(database_service=database_service, credentials=payload)
    if not client.is_configured():
        return jsonify({"error": "Grimmory not configured for this user (set the login above)"}), 400
    try:
        return jsonify(client.get_libraries() or [])
    except Exception as e:
        logger.warning("Per-user Grimmory library lookup failed for user %s: %s", user_id, e, exc_info=True)
        return jsonify({"error": str(e)}), 502


def account_booklore_libraries():
    """List current user's Grimmory libraries from unsaved form credentials."""
    user = current_user()
    if user is None:
        return jsonify({"error": "Authentication required"}), 401
    payload = _posted_user_test_credentials(user, request.get_json(silent=True) or {})
    from src.api.booklore_client import BookloreClient
    client = BookloreClient(database_service=database_service, credentials=payload)
    if not client.is_configured():
        return jsonify({"error": "Grimmory not configured for this user (set the login above)"}), 400
    try:
        return jsonify(client.get_libraries() or [])
    except Exception as e:
        logger.warning("Self-service Grimmory library lookup failed for user %s: %s", user.id, e, exc_info=True)
        return jsonify({"error": str(e)}), 502


# User-management actions accepted by both the legacy /admin/users page and the
# Settings → Users tab (which posts to /settings).
# Every action _apply_user_admin_action handles MUST be listed here: POST /settings
# routes anything missing into the settings-save branch instead, which writes the
# whole settings form and restarts.
_USER_ADMIN_ACTIONS = {'create', 'reset_password', 'toggle_active', 'set_role', 'delete', 'share_library'}


def _apply_user_admin_action(form):
    """Handle a single user-management action (create/reset/toggle/delete).

    Shared by the legacy /admin/users page and the Settings → Users tab.
    Returns (message, error)."""
    message = None
    error = None

    def _active_admin_count():
        return sum(1 for u in database_service.list_users() if u.role == 'admin' and u.active)

    action = form.get('action')
    try:
        if action == 'create':
            username = (form.get('username') or '').strip()
            password = form.get('password') or ''
            role = form.get('role') or 'user'
            if not username or not password:
                error = "Username and password are required"
            elif database_service.get_user_by_username(username):
                error = "That username already exists"
            else:
                new_user = database_service.create_user(username, password, role=role)
                message = f"Created user '{username}'"
                # Share-all-books is about the household seeing one library, so a
                # new account starts with everything rather than only books matched
                # from now on.
                if env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
                    try:
                        linked = database_service.backfill_user_books_for_user(new_user.id)
                        logger.info(
                            "🔗 Shared %d existing book(s) with new user '%s' (share-all-books enabled)",
                            linked, sanitize_log_data(username),
                        )
                        if linked:
                            message = f"Created user '{username}' and shared {linked} book(s)"
                    except Exception as share_err:
                        logger.warning(
                            "Could not backfill shared books for new user '%s': %s",
                            sanitize_log_data(username), share_err, exc_info=True,
                        )
        elif action == 'reset_password':
            uid = int(form.get('user_id'))
            new_pw = form.get('password') or ''
            if not new_pw:
                error = "Password cannot be empty"
            else:
                database_service.set_user_password(uid, new_pw)
                message = "Password reset"
        elif action == 'toggle_active':
            uid = int(form.get('user_id'))
            target = database_service.get_user(uid)
            if target:
                disabling = bool(target.active)
                if disabling and target.role == 'admin' and _active_admin_count() <= 1:
                    error = "Can't disable the last active admin"
                else:
                    database_service.set_user_active(uid, not target.active)
                    try:
                        container.user_client_registry().invalidate(uid)
                    except Exception:
                        pass
                    message = f"{'Disabled' if disabling else 'Enabled'} '{target.username}'"
        elif action == 'set_role':
            uid = int(form.get('user_id'))
            new_role = (form.get('role') or '').strip().lower()
            target = database_service.get_user(uid)
            demoting = new_role == 'user'
            if not target:
                error = "User not found"
            elif new_role not in ('admin', 'user'):
                error = "Invalid role"
            elif target.role == new_role:
                error = f"'{target.username}' is already {new_role}"
            elif demoting and database_service.is_primary_admin(uid):
                # The global service settings are this account mirrored outward
                # (ENGINE_MIRROR_KEYS) and it owns un-scoped rows, so demoting it
                # would leave the engine authenticating as a regular user.
                error = "Can't demote the primary admin — the global service settings belong to that account"
            elif demoting and _active_admin_count() <= 1:
                error = "Can't demote the last active admin"
            else:
                database_service.set_user_role(uid, new_role)
                # The cached client bundle carries the global-fallback flag, which
                # this change flips.
                try:
                    container.user_client_registry().invalidate(uid)
                except Exception:
                    pass
                logger.info(
                    "👤 Role change: '%s' is now '%s'",
                    sanitize_log_data(target.username), new_role,
                )
                if demoting:
                    message = f"'{target.username}' is now a regular user"
                else:
                    message = (
                        f"'{target.username}' is now an admin. They still use their own "
                        f"service logins — set them under Integrations."
                    )
        elif action == 'delete':
            uid = int(form.get('user_id'))
            target = database_service.get_user(uid)
            if not target:
                error = "User not found"
            elif uid == current_user().id:
                error = "You can't delete your own account"
            elif target.role == 'admin' and _active_admin_count() <= 1:
                error = "Can't delete the last active admin"
            else:
                if database_service.delete_user(uid):
                    _clear_match_queue_for_user_id(uid)
                    try:
                        container.user_client_registry().invalidate(uid)
                    except Exception:
                        pass
                    message = f"Deleted '{target.username}'"
                else:
                    error = "User not found"
        elif action == 'share_library':
            if not env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
                error = "Enable Features → Shared Library first, then share the existing catalog."
            else:
                result = database_service.share_all_books_with_active_users()
                links = result.get('links', 0)
                users = result.get('users', 0)
                logger.info(
                    "🔗 Shared %d existing book link(s) across %d active user(s) (share-all-books reconcile)",
                    links, users,
                )
                if links:
                    message = f"Shared {links} book link(s) across {users} user(s)"
                else:
                    message = f"All {users} user(s) already see the full library"
    except Exception as e:
        error = f"Action failed: {e}"
    return message, error


@admin_required
def admin_users():
    """Admin-only user management: create, reset password, enable/disable, delete.

    The primary UI now lives in the Settings → Users tab; this page is the
    direct entry point sharing the same backend."""
    message = None
    error = None
    if request.method == 'POST':
        message, error = _apply_user_admin_action(request.form)

    users = database_service.list_users()
    try:
        primary_admin_id = database_service._default_user_id()
    except Exception:
        primary_admin_id = None
    return render_template(
        'admin_users.html',
        users=users,
        message=message,
        error=error,
        current_user_id=current_user().id,
        primary_admin_id=primary_admin_id,
    )


# ---------------- CONTEXT PROCESSORS ----------------
def inject_global_vars():
    def get_val(key, default_val=None):
        if key in os.environ: return os.environ[key]
        DEFAULTS = {
            'TZ': 'America/New_York',
            'LOG_LEVEL': 'INFO',
            'DATA_DIR': '/data',
            'BOOKS_DIR': '/books',
            'ABS_COLLECTION_NAME': 'Synced with KOReader',
            'BOOKLORE_SHELF_NAME': 'Kobo',
            'SYNC_PERIOD_MINS': '5',
            'SYNC_DELTA_ABS_SECONDS': '60',
            'SYNC_DELTA_KOSYNC_PERCENT': '0.5',
            'SYNC_DELTA_BETWEEN_CLIENTS_PERCENT': '0.5',
            'SYNC_DELTA_KOSYNC_WORDS': '400',
            'FUZZY_MATCH_THRESHOLD': '80',
            'WHISPER_MODEL': 'tiny',
            'JOB_MAX_RETRIES': '5',
            'JOB_RETRY_DELAY_MINS': '15',
            'MONITOR_INTERVAL': '3600',
            'LINKER_BOOKS_DIR': '/linker_books',
            'STORYTELLER_INGEST_DIR': '/linker_books',
            'AUDIOBOOKS_DIR': '/audiobooks',
            'STORYTELLER_ASSETS_DIR': '',
            'ABS_PROGRESS_OFFSET_SECONDS': '0',
            'EBOOK_CACHE_SIZE': '3',
            'ALIGNMENT_CACHE_SIZE': '3',
            'KOSYNC_HASH_METHOD': 'content',
            'TELEGRAM_LOG_LEVEL': 'ERROR',
            'SHELFMARK_URL': '',
            'KOSYNC_ENABLED': 'false',
            'STORYTELLER_ENABLED': 'false',
            'BOOKLORE_ENABLED': 'false',
            'HARDCOVER_ENABLED': 'false',
            'TELEGRAM_ENABLED': 'false',
            'SUGGESTIONS_ENABLED': 'false',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT': 'true'
        }
        if key in DEFAULTS: return DEFAULTS[key]
        return default_val if default_val is not None else ''

    def get_bool(key):
        val = os.environ.get(key, 'false')
        return val.lower() in ('true', '1', 'yes', 'on')

    def get_user_val(key, default_val=''):
        """Per-user setting (the logged-in user's value, else global). Use for
        anything that should reflect THIS user's config, e.g. their reading app."""
        from src.utils.user_config import user_setting
        val = user_setting(key, None)
        return val if val not in (None, '') else default_val

    def get_user_bool(key):
        from src.utils.user_config import user_setting
        return str(user_setting(key, 'false')).lower() in ('true', '1', 'yes', 'on')

    def match_queue_count() -> int:
        """Number of queued Add Book items for the acting user (0 if unavailable).

        Called lazily from the nav so pages without a user context (login, setup)
        never pay the queue read, and a queue failure can never break a render.
        """
        try:
            return len(_load_match_queue())
        except Exception:
            return 0

    return dict(
        shelfmark_url=os.environ.get("SHELFMARK_URL", ""),
        abs_server=_display_abs_server(),
        booklore_server=os.environ.get("BOOKLORE_SERVER", ""),
        get_val=get_val,
        get_bool=get_bool,
        get_user_val=get_user_val,
        get_user_bool=get_user_bool,
        match_queue_count=match_queue_count,
        current_user=current_user(),
    )

# ---------------- BOOK LINKER HELPERS ----------------
from src.services.alignment_service import ingest_storyteller_transcripts









def _run_diagnostics_send(
    force: bool = False,
    manual: bool = False,
    user_message: str = "",
) -> dict:
    """Build service flags and delegate to the diagnostics sender.

    Every flag is computed in its own try/except so a missing or broken
    provider never prevents the heartbeat from reaching the collector.
    """
    def _flag(fn):
        try:
            return bool(fn())
        except Exception:
            return False

    flags = {
        'abs': _flag(lambda: container.abs_client().is_configured()),
        'kosync': env_truthy('KOSYNC_ENABLED'),
        'storyteller': _flag(lambda: container.storyteller_client().is_configured()),
        'booklore': _flag(lambda: container.booklore_client().is_configured()),
        'bookfusion': _flag(lambda: container.bookfusion_client().is_configured()),
        'book_orbit': _flag(lambda: container.bookorbit_client().is_configured()),
        'kavita': _flag(lambda: container.kavita_client().is_configured()),
        'cwa': _flag(lambda: container.cwa_client().is_configured()),
        'hardcover': _flag(lambda: container.hardcover_client().is_configured()),
        'storygraph': _flag(lambda: container.storygraph_client().is_configured()),
        'slash_books': os.path.isdir(os.environ.get('EBOOK_IMPORT_DIR', '/books')),
    }
    try:
        total_books = len(database_service.get_books_by_status('active'))
    except Exception:
        total_books = None

    from src.services import diagnostics
    return diagnostics.maybe_send_diagnostics(
        database_service,
        service_flags=flags,
        total_books=total_books,
        force=force,
        manual=manual,
        user_message=user_message,
    )


def sync_daemon():
    """Background sync daemon running in a separate thread."""
    try:
        # Setup schedule for sync operations
        # Use the global SYNC_PERIOD_MINS which is validated
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(manager.run_sync_for_all_users)
        schedule.every(1).minutes.do(manager.check_pending_jobs)
        schedule.every(1).minutes.do(manager.flush_reading_sessions_for_all_users)
        schedule.every(1).hours.do(_run_diagnostics_send)

        logger.info(f"🔄 Sync daemon started (period: {SYNC_PERIOD_MINS} minutes)")

        # Run initial sync cycle (per user)
        try:
            manager.run_sync_for_all_users()
        except Exception as e:
            logger.error(f"❌ Initial sync cycle failed: {e}", exc_info=True)

        # Catch-up diagnostics send on startup (24h guard inside is a no-op when not due)
        try:
            _run_diagnostics_send()
        except Exception:
            pass

        # Main daemon loop
        while True:
            try:
                # logger.debug("Running pending schedule jobs...")
                schedule.run_pending()
                time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                logger.error(f"❌ Sync daemon error: {e}", exc_info=True)
                time.sleep(60)  # Wait longer on error

    except Exception as e:
        logger.error(f"❌ Sync daemon crashed: {e}", exc_info=True)


# ---------------- ORIGINAL ABS-KOSYNC HELPERS ----------------

def find_ebook_file(filename):
    if not is_plain_basename(filename):
        logger.warning(
            "Refused ebook lookup for a non-basename filename: %s",
            sanitize_log_data(str(filename or "")),
        )
        return None
    base = EBOOK_DIR
    escaped_filename = glob.escape(filename)
    matches = list(base.rglob(escaped_filename))
    return matches[0] if matches else None


def get_kosync_id_for_ebook(ebook_filename, booklore_id=None, original_filename=None,
                            bookorbit_id=None, kavita_id=None, source_path=None):
    """Get KOSync document ID for an ebook.
    Tries Grimmory API first (if configured and booklore_id provided),
    falls back to filesystem, then BookOrbit / ABS / CWA on-demand downloads.
    """
    # Try Grimmory API first
    clients = uc()

    if kavita_id and clients.kavita_client.is_configured():
        try:
            book = clients.kavita_client.get_book_by_id(kavita_id, allow_refresh=False)
            source_hash = str((book or {}).get('koreader_hash') or '').strip()
            if source_hash:
                return source_hash
            content = clients.kavita_client.download_book(kavita_id)
            if content:
                kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    return kosync_id
        except Exception as e:
            logger.warning("Failed to get KOSync ID from Kavita: %s", e, exc_info=True)

    if booklore_id and clients.booklore_client.is_configured():
        try:
            content = clients.booklore_client.download_book(booklore_id)
            if content:
                kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    logger.debug(f"🔍 Computed KOSync ID from Grimmory download: '{kosync_id}'")
                    return kosync_id
        except Exception as e:
            logger.warning(f"⚠️ Failed to get KOSync ID from Grimmory, falling back to filesystem: {e}", exc_info=True)

    # Fall back to filesystem. When a queue item carries the selected local source path,
    # prefer it over filename-only globbing so duplicate basenames do not hash the wrong book.
    ebook_path = None
    source_path_text = _safe_local_source_path(source_path) if "://" not in str(source_path or "") else ""
    if source_path_text:
        try:
            selected_path = Path(source_path_text)
            if selected_path.exists() and selected_path.is_file():
                ebook_path = selected_path
            else:
                logger.debug(f"Selected ebook source path not found, falling back to filename search: '{source_path_text}'")
        except Exception as e:
            logger.debug(f"Selected ebook source path could not be used, falling back to filename search: {e}")
    if not ebook_path:
        ebook_path = find_ebook_file(ebook_filename)
    if not ebook_path and original_filename:
        # [Tri-Link] Fallback to original filename if Storyteller file not found/relevant
        logger.debug(f"Primary file '{ebook_filename}' not found, checking original '{original_filename}'")
        ebook_path = find_ebook_file(original_filename)

    if ebook_path:
        return container.ebook_parser().get_kosync_id(ebook_path)

    # Check the EPUB cache explicitly when LibraryService acquired a file outside /books.
    epub_cache = container.epub_cache_dir()
    for cache_filename in dict.fromkeys((ebook_filename, original_filename)):
        if not cache_filename:
            continue
        cached_path = safe_cache_path(epub_cache, cache_filename)
        if cached_path and cached_path.exists():
            return container.ebook_parser().get_kosync_id(cached_path)

    # On-demand fetching
    # 0. BookOrbit On-Demand — the library hosts the file via API even when the
    #    shared /books volume isn't mounted. Resolve the book id (passed by the
    #    match flow, else by filename search) and hash the downloaded bytes.
    bookorbit_client = clients.bookorbit_client
    if bookorbit_client and bookorbit_client.is_configured():
        try:
            bo_id = bookorbit_id
            if not bo_id:
                bo_book = bookorbit_client.find_book_by_filename(ebook_filename)
                bo_id = bo_book.get('id') if bo_book else None
            if bo_id:
                logger.info(f"📥 Attempting on-demand BookOrbit download for '{bo_id}'")
                content = bookorbit_client.download_book(bo_id)
                if content:
                    kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                    if kosync_id:
                        logger.debug(f"🔍 Computed KOSync ID from BookOrbit download: '{kosync_id}'")
                        if cached_path:
                            try:
                                if not epub_cache.exists():
                                    epub_cache.mkdir(parents=True, exist_ok=True)
                                cached_path.write_bytes(content)
                                logger.info(f"   ✅ Cached BookOrbit download to '{cached_path}'")
                            except Exception as cache_err:
                                logger.warning(f"⚠️ Failed to cache BookOrbit download: {cache_err}", exc_info=True)
                        return kosync_id
        except Exception as e:
            logger.warning(f"⚠️ Failed to get KOSync ID from BookOrbit: {e}", exc_info=True)

    # 1. ABS On-Demand
    if "_abs." in ebook_filename:
        try:
             # Extract ID: 1941a138-1c8d-49eb-954f-f6bb26f87ebc_abs.epub -> 1941a138-1c8d-49eb-954f-f6bb26f87ebc
             abs_id = ebook_filename.split("_abs.")[0]
             abs_client = clients.abs_client
             if abs_client and abs_client.is_configured():
                 logger.info(f"📥 Attempting on-demand ABS download for '{abs_id}'")
                 ebook_files = abs_client.get_ebook_files(abs_id)
                 if ebook_files:
                     target = ebook_files[0]
                     if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                     
                     if cached_path and abs_client.download_file(target['stream_url'], cached_path):
                         logger.info(f"   ✅ Downloaded ABS ebook to '{cached_path}'")
                         return container.ebook_parser().get_kosync_id(cached_path)
                 else:
                     logger.warning(f"   ⚠️ No ebook files found in ABS for item '{abs_id}'")
        except Exception as e:
            logger.error(f"   ❌ Failed ABS on-demand download: {e}", exc_info=True)

    # 2. CWA On-Demand
    if "_cwa." in ebook_filename or ebook_filename.startswith("cwa_"):
        try:
             # Extract ID: cwa_12345.epub -> 12345
             # Format is cwa_{id}.{ext}
             if ebook_filename.startswith("cwa_"):
                 # Robust: strip cwa_ prefix and the extension
                 cwa_id = ebook_filename[4:].rsplit(".", 1)[0]
             else:
                 # Pattern like somefile_cwa.epub or itemid_cwa.epub
                 cwa_id = ebook_filename.split("_cwa.")[0]
                 
                 # If it was still prefixed with something else, handle it? 
                 # Usually it's {uuid}_cwa.epub or cwa_{id}.epub
                 if "_" in cwa_id and not ebook_filename.startswith("cwa_"):
                     # If format is uuid_cwa.epub, cwa_id is uuid (correct)
                     pass 
                 
             if cwa_id:
                 cwa_client = clients.cwa_client
                 if cwa_client and cwa_client.is_configured():
                     logger.info(f"📥 Attempting on-demand CWA download for ID '{cwa_id}'")
                     
                     target = None
                     
                     # Priority 1: Search for the ID (search results include download_url and won't crash the server)
                     results = cwa_client.search_ebooks(cwa_id)
                     
                     # Find exact ID match if possible
                     for res in results:
                         if str(res.get('id')) == cwa_id:
                             target = res
                             break
                     
                     # If no exact ID match, maybe it was the only result
                     if not target and len(results) == 1:
                         target = results[0]

                     # Priority 2: Use direct download URL from search if available
                     if target and target.get('download_url'):
                         logger.info(f"🚀 Using direct download link from search for '{target.get('title', 'Unknown')}'")
                     else:
                         # Priority 3: Fallback to get_book_by_id only if search didn't provide a URL
                         # This may crash server on metadata page, but includes a blind URL fallback
                         logger.debug(f"🔍 Search did not return a usable result, trying direct ID lookup")
                         target = cwa_client.get_book_by_id(cwa_id)

                     if target and target.get('download_url'):
                         if not epub_cache.exists(): epub_cache.mkdir(parents=True, exist_ok=True)
                         if cached_path and cwa_client.download_ebook(target['download_url'], cached_path):
                             logger.info(f"   ✅ Downloaded CWA ebook to '{cached_path}'")
                             return container.ebook_parser().get_kosync_id(cached_path)
                     else:
                         logger.warning(f"   ⚠️ Could not find CWA book for ID '{cwa_id}'")
        except Exception as e:
            logger.error(f"   ❌ Failed CWA on-demand download: {e}", exc_info=True)

    # Neither source available - log helpful warning
    if (not clients.booklore_client.is_configured()
            and not clients.bookorbit_client.is_configured()
            and not _optional_client_configured(clients, "kavita_client")
            and not EBOOK_DIR.exists()):
        logger.warning(
            f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': "
            "No ebook source configured. Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, "
            "BOOKLORE_PASSWORD), enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, "
            "BOOKORBIT_PASSWORD), enable Kavita (KAVITA_SERVER, KAVITA_API_KEY), "
            "or mount the ebooks directory to /books"
        )
    elif not booklore_id and not ebook_path:
        logger.warning(f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': File not found in Grimmory, filesystem, or remote sources")

    return None


def _compute_storyteller_trilink_kosync_id(original_ebook_filename, storyteller_filename, log_prefix):
    """Prefer the original EPUB hash for Tri-Link, but fall back to the Storyteller artifact."""
    booklore_id = None
    if original_ebook_filename:
        logger.info(f"⚡ {log_prefix}: Computing hash from original EPUB '{original_ebook_filename}'")
        if uc().booklore_client.is_configured():
            bl_book = uc().booklore_client.find_book_by_filename(original_ebook_filename)
            if bl_book:
                booklore_id = bl_book.get('id')

        kosync_doc_id = get_kosync_id_for_ebook(original_ebook_filename, booklore_id)
        if kosync_doc_id:
            return kosync_doc_id

        logger.warning(
            f"⚠️ {log_prefix}: Could not compute hash from original EPUB "
            f"'{sanitize_log_data(original_ebook_filename)}'; falling back to Storyteller artifact"
        )
    else:
        logger.info(f"⚡ {log_prefix}: No original EPUB available; using Storyteller artifact")

    logger.info(f"⚡ {log_prefix}: Computing hash from downloaded Storyteller artifact '{storyteller_filename}'")
    return get_kosync_id_for_ebook(storyteller_filename)


def _is_storyteller_artifact_filename(filename):
    if not isinstance(filename, str):
        return False
    return bool(filename and re.match(r"^storyteller_[0-9a-fA-F-]+\.epub$", filename))


def _is_abs_hosted_ebook_filename(filename) -> bool:
    """True for the synthesized name of an ABS-hosted ebook (`<abs_id>_abs.epub`).

    The `_abs.` marker is the same one `get_kosync_id_for_ebook` reads the ABS item
    id back out of, so an ebook wearing it lives in ABS, not in a library with
    shelves.
    """
    if not isinstance(filename, str):
        return False
    return "_abs." in filename

def _shelve_matched_ebook(shelf_filename, ebook_source=None, ebook_source_id=None):
    """Add a newly matched ebook to the Kobo shelf and clear it from the
    shelf-watch "Up Next" shelf, on whichever library hosts the ebook.

    Auto-matching moves a book Up Next -> Kobo, but approving a suggestion (or a
    manual match) historically only added to Kobo and left the book sitting on Up
    Next. Once the book is stored as a match it no longer belongs in the to-read
    queue, so mirror the auto-match behaviour here. The Up Next removal is gated on
    the shelf-watch feature being enabled to avoid touching shelves for users who
    don't use it.
    """
    if not shelf_filename:
        return

    from src.utils.user_config import user_setting
    clients = uc()
    source = (ebook_source or "").strip().lower()
    is_bookorbit = source == "bookorbit"
    is_kavita = source == "kavita"
    if is_bookorbit:
        client = clients.bookorbit_client
        kobo_shelf = (user_setting("BOOKORBIT_SHELF_NAME") or "Kobo").strip()
        watch_enabled = str(os.environ.get("BOOKORBIT_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (os.environ.get("BOOKORBIT_SHELF_WATCH_NAME") or "Up Next").strip()
    elif is_kavita:
        client = clients.kavita_client
        kobo_shelf = (user_setting("KAVITA_COLLECTION_NAME") or "BookBridge").strip()
        watch_enabled = str(os.environ.get("KAVITA_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (os.environ.get("KAVITA_SHELF_WATCH_NAME") or "Up Next").strip()
    else:
        # Grimmory owns this branch; it is not a catch-all. An ABS- or
        # BookFusion-hosted ebook has no Grimmory catalog entry, so the shelf call
        # can only ever warn "Book not found for shelf assignment/removal" — and it
        # does so every cycle, forever (#4184, #4187). ABS-hosted books are already
        # shelved by their own add_to_collection call. An unrecorded source stays on
        # this path: legacy rows and /books-mount installs never set one.
        if source not in ("", "booklore", "grimmory") or _is_abs_hosted_ebook_filename(shelf_filename):
            logger.debug(
                f"Skipping Grimmory shelf ops for '{sanitize_log_data(shelf_filename)}': "
                f"hosted by {ebook_source or 'ABS'}, not Grimmory"
            )
            return
        client = clients.booklore_client
        kobo_shelf = user_setting("BOOKLORE_SHELF_NAME", "Kobo")
        watch_enabled = str(os.environ.get("BOOKLORE_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (os.environ.get("BOOKLORE_SHELF_WATCH_NAME") or "Up Next").strip()

    if not client.is_configured():
        return

    # Prefer the known BookOrbit book id (filenames like "Title - Author.epub"
    # don't reliably resolve via BookOrbit's title-based search).
    use_id = (is_bookorbit or is_kavita) and ebook_source_id and hasattr(client, "add_book_id_to_shelf")
    try:
        if use_id:
            added = client.add_book_id_to_shelf(ebook_source_id, kobo_shelf)
        else:
            added = client.add_to_shelf(shelf_filename, kobo_shelf)
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to add '{sanitize_log_data(shelf_filename)}' to '{kobo_shelf}': {e}",
            exc_info=True
        )
        return
    if not added:
        logger.warning(
            f"⚠️ Failed to add '{sanitize_log_data(shelf_filename)}' to '{kobo_shelf}'"
        )
        return

    same_shelf = (
        client._shelf_key(watch_shelf) == client._shelf_key(kobo_shelf)
        if is_bookorbit or is_kavita else watch_shelf == kobo_shelf
    )
    if watch_enabled and watch_shelf and not same_shelf:
        try:
            if use_id:
                client.remove_book_id_from_shelf(ebook_source_id, watch_shelf)
            else:
                client.remove_from_shelf(shelf_filename, watch_shelf)
        except Exception as e:
            logger.warning(
                f"⚠️ Failed to remove '{sanitize_log_data(shelf_filename)}' from watch shelf '{watch_shelf}': {e}",
                exc_info=True
            )


def _shelve_saved_ebook(book) -> None:
    """Shelve a saved mapping's original ebook through its owning library."""
    if not book:
        return
    shelf_filename = (
        getattr(book, 'original_ebook_filename', None)
        or getattr(book, 'ebook_filename', None)
    )
    if not shelf_filename or _is_storyteller_artifact_filename(shelf_filename):
        return
    _shelve_matched_ebook(
        shelf_filename,
        getattr(book, 'ebook_source', None),
        getattr(book, 'ebook_source_id', None),
    )
    _spawn_user_background(_publish_saved_ebook_to_readest, book, label="Readest upload")


def _publish_saved_ebook_to_readest(book) -> None:
    """Upload a saved mapping's original ebook into the user's Readest cloud library.

    Best-effort convenience layered on top of the match-save flow: gated on the
    per-user `READEST_UPLOAD_ON_MATCH` setting (off by default) and must never
    affect the caller, so every failure is swallowed here and only logged. Runs
    off the request thread via `_spawn_user_background`, which re-binds the
    ambient user id/credentials onto the worker thread — there is no Flask
    request context here to resolve them from.
    """
    if not book:
        return
    filename = (
        getattr(book, 'original_ebook_filename', None)
        or getattr(book, 'ebook_filename', None)
    )
    if not filename or _is_storyteller_artifact_filename(filename):
        return

    def _on(key, default):
        return (user_setting(key, default) or "").strip().lower() in ("true", "1", "yes", "on")

    # The service gate covers every Readest feature, so an install-wide off here
    # stops the upload even for a user who left it switched on.
    if not _on("READEST_ENABLED", "true") or not _on("READEST_UPLOAD_ON_MATCH", "false"):
        return

    user_id = get_current_user_id()
    creds = get_current_user_credentials()

    try:
        from src.api.readest_client import ReadestClient
        from src.services.readest_upload_service import ReadestUploadService

        client = ReadestClient(credentials=creds, database_service=database_service, user_id=user_id)
        service = ReadestUploadService(client, container.ebook_parser(), database_service)
        result = service.publish_book(filename)
        logger.info(
            "📚 Readest publish for %s: status=%s message=%s",
            filename, result.status, result.message,
        )
    except Exception as e:
        logger.error("Readest publish failed for %s: %s", filename, e, exc_info=True)


def _download_storyteller_artifact(storyteller_uuid, abs_title=None, *, original_ebook_filename=None):
    """Resolve a Storyteller artifact path.

    When ``STORYTELLER_NO_EPUB_CACHE`` is enabled and an original EPUB can be
    located via ``EbookParser.resolve_book_path``, skip the API download and
    return ``(original_name, original_path)``. Otherwise, download the
    Storyteller ReadAloud EPUB into the epub cache as a slim, audio-stripped copy,
    falling back to a local ``STORYTELLER_LIBRARY_DIR`` copy (also stripped) on
    failure.

    Returns ``(filename, Path)`` on success, ``(None, None)`` on failure.
    """
    epub_cache = container.epub_cache_dir()
    epub_cache.mkdir(parents=True, exist_ok=True)

    artifact_filename = f"storyteller_{storyteller_uuid}.epub"
    target_path = epub_cache / artifact_filename

    no_epub_cache = env_truthy("STORYTELLER_NO_EPUB_CACHE")
    if no_epub_cache and original_ebook_filename:
        original_name = Path(str(original_ebook_filename)).name
        nocache_candidates = [epub_cache / original_name]
        try:
            nocache_candidates.append(container.ebook_parser().resolve_book_path(original_name))
        except Exception:
            pass

        resolved = None
        for candidate in nocache_candidates:
            try:
                if candidate and Path(candidate).exists():
                    resolved = Path(candidate)
                    break
            except Exception:
                continue

        if resolved:
            logger.info(
                "📦 Storyteller download: STORYTELLER_NO_EPUB_CACHE=true; using original EPUB '%s'",
                resolved.name,
            )
            return resolved.name, resolved
        logger.warning(
            "📦 Storyteller download: STORYTELLER_NO_EPUB_CACHE=true but no original EPUB found "
            "for '%s'; falling back to Storyteller ReadAloud download",
            original_name,
        )

    downloaded = False
    try:
        downloaded = uc().storyteller_client.download_slim_book(storyteller_uuid, target_path)
    except Exception as dl_err:
        logger.warning(f"Storyteller API download failed for '{storyteller_uuid}': {dl_err}", exc_info=True)

    if downloaded:
        return artifact_filename, target_path

    st_lib = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
    if abs_title and st_lib.exists():
        for child in st_lib.iterdir():
            if not child.is_dir():
                continue
            readaloud = list(child.glob("*readaloud*.epub")) + list(child.glob("*synced*/*.epub"))
            if readaloud and child.name.lower().strip() == abs_title.lower().strip():
                try:
                    uc().storyteller_client._strip_audio_from_epub(readaloud[0], target_path)
                except Exception as strip_err:
                    logger.warning(
                        f"Storyteller local fallback strip failed for '{readaloud[0]}'; "
                        f"copying verbatim: {strip_err}",
                        exc_info=True,
                    )
                    shutil.copy2(readaloud[0], target_path)
                logger.warning(f"Storyteller local fallback used: '{readaloud[0]}'")
                return artifact_filename, target_path

    return None, None


def _resolve_abs_chapters_for_storyteller_ingest(book):
    if not book or getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return []
    try:
        item_details = uc().abs_client.get_item_details(book.abs_id)
    except Exception as abs_err:
        logger.warning(f"Failed ABS chapter lookup for storyteller ingest '{book.abs_id}': {abs_err}", exc_info=True)
        return []
    if not item_details:
        return []
    return item_details.get("media", {}).get("chapters", []) or []


def _preserve_or_reset_mapping_status(
    target_book,
    *,
    kosync_doc_id=None,
    ebook_filename=None,
    audio_source_id=None,
    storyteller_uuid=None,
    ebook_source=None,
    ebook_source_id=None,
) -> None:
    """Queue a mapping for processing, unless its existing alignment still applies.

    Re-matching used to reset every mapping to 'pending' unconditionally, which sent
    an already-aligned book back through transcription and alignment. That is what a
    second user hit when adopting a book someone else had already matched: the
    catalog row and its alignment are shared, so the only thing they actually needed
    was the per-user claim.

    An alignment is only reusable when the pairing it was built from is unchanged, so
    every identity value the caller is about to overwrite is compared first — a
    genuine re-map (different ebook or different audio) still re-runs the pipeline.
    Mirrors the same decision in SyncManager's clear-progress path.

    MUST be called before the caller overwrites those attributes.
    """
    if target_book is None:
        return

    abs_id = getattr(target_book, "abs_id", None)
    # Every identity value a caller may overwrite has to be listed here: a field
    # that is set later but never compared silently keeps a stale alignment. The
    # readalong uuid and the ebook source id are as much a part of the pairing as
    # the filename — swapping a book to a different Storyteller readalong changes
    # the audio the map was built against.
    existing_source = _normalize_text_source_type(getattr(target_book, "ebook_source", None))
    next_source = _normalize_text_source_type(ebook_source) if ebook_source is not None else existing_source
    existing_source_id = str(getattr(target_book, "ebook_source_id", None) or "").strip()
    next_source_id = str(ebook_source_id or "").strip()
    same_stable_ebook = bool(
        existing_source
        and next_source
        and existing_source.lower() == next_source.lower()
        and existing_source_id
        and next_source_id
        and existing_source_id == next_source_id
    )

    candidates = (
        ("kosync_doc_id", kosync_doc_id),
        ("ebook_filename", None if same_stable_ebook else ebook_filename),
        ("audio_source_id", audio_source_id),
        ("storyteller_uuid", storyteller_uuid),
        ("ebook_source_id", ebook_source_id),
    )

    changed = []
    for attr, new_value in candidates:
        if new_value is None:
            continue
        existing = getattr(target_book, attr, None)
        # Only a value that actually differs counts. A field going from empty to
        # populated is deliberately NOT a change: legacy rows have blanks that get
        # backfilled with the same effective pairing, and treating that as a re-map
        # would re-transcribe books this guard exists to spare.
        if existing and str(existing) != str(new_value):
            changed.append(attr)

    if ebook_source is not None and existing_source and existing_source.lower() != next_source.lower():
        changed.append("ebook_source")

    if same_stable_ebook and not changed:
        logger.info(
            "♻️ '%s' External ebook metadata changed with stable source identity — "
            "preserving mapping status",
            sanitize_log_data(abs_id),
        )
        return

    reusable = False
    if not changed and abs_id:
        try:
            reusable = database_service.has_alignment(abs_id)
        except Exception as align_err:
            logger.warning(
                "Could not check for an existing alignment on '%s': %s",
                sanitize_log_data(abs_id), align_err, exc_info=True,
            )
            reusable = False

    if reusable:
        if getattr(target_book, "status", None) != "active":
            target_book.status = "active"
        logger.info(
            "♻️ '%s' Re-match reuses the existing alignment — keeping the mapping active "
            "(no re-transcription)",
            sanitize_log_data(abs_id),
        )
        return

    if changed:
        logger.info(
            "🔄 '%s' Mapping identity changed (%s) — queuing for re-processing",
            sanitize_log_data(abs_id), ", ".join(changed),
        )
    target_book.status = "pending"


def _adopt_kosync_progress_for_book(abs_id: str, kosync_doc_id: str) -> None:
    """Link an already-known KoSync document hash to a freshly mapped book.

    KOReader stores progress under a document hash before the book is mapped. The
    resolution path reaches that progress by joining KosyncDocument.linked_abs_id, so
    an unlinked row stays invisible to sync and the book reads as unstarted even
    though the position is durable and the hashes match exactly (#431). Add Book
    already computes the same hash, so adopt it here rather than leaving the reader
    to link it by hand.

    Adoption is fail-closed: a hash already owned by a *different* book is left
    alone, matching the sibling-hash step below and `_register_hash_for_book`.
    `ensure_linked_kosync_document` re-points on conflict by design — hash
    reconciliation needs that to keep sibling hashes of one book durable (#285) —
    so the ownership check belongs here, at the one caller that must not steal.
    Re-pointing would hide the loser's stored progress behind the same join this
    function exists to repair, moving #431 rather than fixing it. The mapping paths
    that legitimately consolidate a duplicate (`match`, `absorb_duplicate_mapping`,
    the ebook-only tri-link) all migrate and delete the loser first, which clears
    its link, so they reach this with nothing to conflict against.
    """
    if not abs_id or not isinstance(kosync_doc_id, str) or not kosync_doc_id.strip():
        return
    try:
        doc_id = kosync_doc_id.strip()
        existing = database_service.get_kosync_document(doc_id)
        owner = getattr(existing, "linked_abs_id", None) if existing else None
        if owner and owner != abs_id:
            logger.info(
                "🔒 KoSync document '%s' already belongs to '%s' — not re-pointing it to '%s'",
                sanitize_log_data(doc_id), sanitize_log_data(owner), sanitize_log_data(abs_id),
            )
            return
        if database_service.ensure_linked_kosync_document(doc_id, abs_id):
            logger.info(
                "🔗 Adopted existing KoSync document '%s' for '%s'",
                sanitize_log_data(kosync_doc_id), sanitize_log_data(abs_id),
            )
    except Exception as e:
        logger.warning(f"⚠️ Failed to adopt KoSync document for '{abs_id}': {e}", exc_info=True)


def _upsert_storyteller_mapping(
    *,
    mode_hint,
    abs_id=None,
    abs_title=None,
    storyteller_uuid=None,
    ebook_filename=None,
    ebook_source=None,
    ebook_source_id=None,
    existing_book=None,
    duration=None,
):
    """
    Shared Storyteller/ebook mapping upsert for:
    - existing row updates (modal link + match-based link updates)
    - ebook-only creation from Match when no audiobook is selected
    """
    if mode_hint not in {"existing", "ebook_only_create"}:
        raise ValueError(f"Unsupported mode_hint: {mode_hint}")

    selected_storyteller_uuid = (storyteller_uuid or "").strip() or None
    selected_ebook_filename = (ebook_filename or "").strip() or None
    selected_ebook_source = _normalize_text_source_type(ebook_source)
    selected_ebook_source_id = str(ebook_source_id or "").strip() or None
    requested_abs_id = str(abs_id or "").strip() or None
    if selected_ebook_source == "BookFusion":
        return None, "BookFusion links require an audiobook mapping", 400

    target_book = existing_book
    if mode_hint == "existing":
        if target_book is None and requested_abs_id:
            target_book = database_service.get_book(requested_abs_id)
        if not target_book:
            return None, "Book not found", 404

    original_ebook_filename = selected_ebook_filename
    if not original_ebook_filename and target_book and target_book.original_ebook_filename:
        original_ebook_filename = target_book.original_ebook_filename
    if (
        not original_ebook_filename
        and target_book
        and target_book.ebook_filename
        and not _is_storyteller_artifact_filename(target_book.ebook_filename)
    ):
        original_ebook_filename = target_book.ebook_filename

    resolved_ebook_filename = selected_ebook_filename or (target_book.ebook_filename if target_book else None)

    if selected_storyteller_uuid:
        artifact_filename, _artifact_path = _download_storyteller_artifact(
            selected_storyteller_uuid,
            abs_title,
            original_ebook_filename=original_ebook_filename,
        )
        if not artifact_filename:
            return None, "Failed to download Storyteller artifact", 500
        resolved_ebook_filename = artifact_filename

    if not resolved_ebook_filename:
        return None, "Please select a text source (Storyteller or Standard Ebook)", 400

    kosync_doc_id = None
    if selected_storyteller_uuid:
        log_prefix = "Storyteller link" if mode_hint == "existing" else "Ebook-only Tri-Link"
        kosync_doc_id = _compute_storyteller_trilink_kosync_id(
            original_ebook_filename,
            resolved_ebook_filename,
            log_prefix,
        )
        if not kosync_doc_id and target_book and target_book.kosync_doc_id:
            logger.warning(
                "Storyteller link hash fallback failed for '%s'; preserving existing hash '%s'",
                sanitize_log_data(target_book.abs_id),
                target_book.kosync_doc_id,
            )
            kosync_doc_id = target_book.kosync_doc_id
    else:
        booklore_id = None
        if uc().booklore_client.is_configured():
            bl_book = uc().booklore_client.find_book_by_filename(resolved_ebook_filename)
            if bl_book:
                booklore_id = bl_book.get("id")
        kosync_doc_id = get_kosync_id_for_ebook(
            resolved_ebook_filename,
            booklore_id,
            bookorbit_id=selected_ebook_source_id if selected_ebook_source == "BookOrbit" else None,
            kavita_id=selected_ebook_source_id if selected_ebook_source == "Kavita" else None,
        )
        if not kosync_doc_id and target_book and target_book.kosync_doc_id:
            kosync_doc_id = target_book.kosync_doc_id

    if not isinstance(kosync_doc_id, str) or not kosync_doc_id.strip():
        kosync_doc_id = None

    if not kosync_doc_id:
        if mode_hint == "existing":
            kosync_doc_id = target_book.kosync_doc_id if target_book else None
            logger.warning(
                "Proceeding without recomputed KOSync hash for existing mapping '%s'",
                sanitize_log_data(abs_id or (target_book.abs_id if target_book else "")),
            )
        else:
            return None, "Could not compute KOSync ID for ebook", 404

    created_ebook_only = False
    migration_source_id = None
    if mode_hint == "ebook_only_create":
        existing_by_hash = database_service.get_book_by_kosync_id(kosync_doc_id)
        preferred_abs_id = requested_abs_id
        if not preferred_abs_id and selected_ebook_source == "ABS" and selected_ebook_source_id:
            preferred_abs_id = selected_ebook_source_id

        if existing_by_hash:
            if preferred_abs_id and existing_by_hash.abs_id != preferred_abs_id:
                migration_source_id = existing_by_hash.abs_id
                target_book = database_service.get_book(preferred_abs_id)
                if not target_book:
                    from src.db.models import Book

                    target_book = Book(
                        abs_id=preferred_abs_id,
                        abs_title=existing_by_hash.abs_title or abs_title,
                        sync_mode=getattr(existing_by_hash, "sync_mode", "ebook_only") or "ebook_only",
                    )
                    created_ebook_only = True
                for attr in (
                    "audio_source",
                    "audio_source_id",
                    "audio_title",
                    "audio_cover_url",
                    "audio_duration",
                    "audio_provider_book_id",
                    "audio_provider_file_id",
                    "ebook_filename",
                    "ebook_source",
                    "ebook_source_id",
                    "original_ebook_filename",
                    "transcript_file",
                    "transcript_source",
                    "storyteller_uuid",
                    "abs_ebook_item_id",
                    "duration",
                    "status",
                ):
                    existing_value = getattr(existing_by_hash, attr, None)
                    if existing_value and not getattr(target_book, attr, None):
                        setattr(target_book, attr, existing_value)
                logger.info(
                    "Match ebook-only create: migrating mapping '%s' -> '%s' for hash '%s'",
                    sanitize_log_data(existing_by_hash.abs_id),
                    sanitize_log_data(preferred_abs_id),
                    kosync_doc_id,
                )
            else:
                target_book = existing_by_hash
                logger.info(
                    "Match ebook-only create: reusing existing mapping '%s' for hash '%s'",
                    sanitize_log_data(target_book.abs_id),
                    kosync_doc_id,
                )
        if not target_book:
            from src.db.models import Book

            target_abs_id = preferred_abs_id or f"ebook-{kosync_doc_id[:16]}"
            target_book = database_service.get_book(target_abs_id)
            if not target_book:
                inferred_title = abs_title or Path(resolved_ebook_filename).stem or target_abs_id
                target_book = Book(
                    abs_id=target_abs_id,
                    abs_title=inferred_title,
                    sync_mode="ebook_only",
                )
                created_ebook_only = True
                logger.info(
                    "Match ebook-only create: creating new mapping '%s' for '%s'",
                    sanitize_log_data(target_abs_id),
                    sanitize_log_data(inferred_title),
                )

    if not target_book:
        return None, "Book not found", 404

    _preserve_or_reset_mapping_status(
        target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        storyteller_uuid=selected_storyteller_uuid,
        ebook_source=selected_ebook_source,
        ebook_source_id=selected_ebook_source_id,
    )
    target_book.abs_title = abs_title or target_book.abs_title or Path(resolved_ebook_filename).stem
    target_book.ebook_filename = resolved_ebook_filename
    target_book.kosync_doc_id = kosync_doc_id
    if selected_ebook_source:
        target_book.ebook_source = selected_ebook_source
    if selected_ebook_source_id:
        target_book.ebook_source_id = selected_ebook_source_id
        if selected_ebook_source == "ABS":
            target_book.abs_ebook_item_id = selected_ebook_source_id

    if original_ebook_filename:
        target_book.original_ebook_filename = original_ebook_filename
    elif mode_hint == "ebook_only_create" and not getattr(target_book, "original_ebook_filename", None):
        if not _is_storyteller_artifact_filename(resolved_ebook_filename):
            target_book.original_ebook_filename = resolved_ebook_filename

    if duration is not None:
        target_book.duration = duration

    if mode_hint == "ebook_only_create":
        if created_ebook_only or getattr(target_book, "sync_mode", "audiobook") == "ebook_only" or str(target_book.abs_id).startswith("ebook-"):
            target_book.sync_mode = "ebook_only"
        else:
            logger.info(
                "Match ebook-only create reused ABS-backed mapping '%s'; keeping sync_mode='%s'",
                sanitize_log_data(target_book.abs_id),
                getattr(target_book, "sync_mode", "audiobook"),
            )

    if selected_storyteller_uuid:
        chapters = _resolve_abs_chapters_for_storyteller_ingest(target_book)
        if getattr(target_book, "sync_mode", "audiobook") == "ebook_only":
            logger.info(
                "Storyteller ingest chapterless mode selected for ebook-only mapping '%s'",
                sanitize_log_data(target_book.abs_id),
            )
        storyteller_manifest = ingest_storyteller_transcripts(
            target_book.abs_id,
            target_book.abs_title or "",
            chapters,
        )
        target_book.storyteller_uuid = selected_storyteller_uuid
        target_book.transcript_file = storyteller_manifest
        target_book.transcript_source = _storyteller_transcript_source(
            selected_storyteller_uuid,
            storyteller_manifest,
        )

    saved_book = database_service.save_book(target_book)
    if not isinstance(getattr(saved_book, "abs_id", None), str):
        saved_book = target_book

    if migration_source_id and migration_source_id != saved_book.abs_id:
        try:
            database_service.migrate_book_data(migration_source_id, saved_book.abs_id)
            database_service.delete_book(migration_source_id)
            logger.info(
                "Match ebook-only create: migrated '%s' into '%s'",
                sanitize_log_data(migration_source_id),
                sanitize_log_data(saved_book.abs_id),
            )
        except Exception as merge_err:
            logger.error(
                "Match ebook-only create: failed to migrate '%s' into '%s': %s",
                sanitize_log_data(migration_source_id),
                sanitize_log_data(saved_book.abs_id),
                merge_err,
                exc_info=True,
            )

    if selected_storyteller_uuid and uc().storyteller_client.is_configured():
        try:
            uc().storyteller_client.add_to_collection_by_uuid(selected_storyteller_uuid)
        except Exception as st_err:
            logger.warning(f"Failed to add Storyteller UUID to collection: {st_err}", exc_info=True)

    if getattr(saved_book, "sync_mode", "audiobook") == "ebook_only":
        logger.info("Skipping ABS collection side effects for ebook-only mapping '%s'", saved_book.abs_id)

    # Auto-match progress trackers at creation (deferred to the background worker so the
    # Match page redirects immediately). Idempotent (each client early-returns if already
    # linked); this closes the gap where ebook-only creates only matched on a later sync
    # cycle, so a freshly linked BookOrbit/KOReader book gets Hardcover/StoryGraph too.
    try:
        tracker_clients = uc().sync_clients or {}
    except Exception:
        tracker_clients = {}
    _enqueue_tracker_automatch(tracker_clients, saved_book)

    database_service.dismiss_suggestion(saved_book.abs_id)
    if isinstance(saved_book.kosync_doc_id, str) and saved_book.kosync_doc_id.strip():
        database_service.dismiss_suggestion(saved_book.kosync_doc_id)
    _adopt_kosync_progress_for_book(saved_book.abs_id, saved_book.kosync_doc_id)

    return saved_book, None, None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Grimmory, CWA, ABS, or filesystem."""

    def __init__(self, name, title=None, subtitle=None, authors=None, booklore_id=None, path=None, source=None, source_id=None, abs_identifier=None, language=None):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.language = str(language or '').strip()
        self.booklore_id = booklore_id
        self.path = path # Public path
        self.source = source  # 'booklore', 'cwa', 'abs', 'filesystem'
        self.source_id = source_id or booklore_id # Generic ID for any source
        self.abs_identifier = abs_identifier  # audiobookshelf_id from Calibre identifiers, if known
        # Has metadata if we have a real title (not just filename) or booklore_id
        self.has_metadata = booklore_id is not None or (title is not None and title != name)

    @property
    def display_name(self):
        """Format: 'Title: Subtitle - Author' for sources with metadata, title for filesystem."""
        if self.has_metadata and self.title:
            full_title = self.title
            if self.subtitle:
                full_title = f"{self.title}: {self.subtitle}"
            if self.authors:
                return f"{full_title} - {self.authors}"
            return full_title
        return self.title

    @property
    def stem(self):
        return Path(self.name).stem

    def __str__(self):
        return self.name


def _ebook_edition_label(book: dict) -> str:
    """Return a short edition label that distinguishes same-titled books.

    Priority:
    1. Non-empty subtitle -> return it stripped.
    2. Series name present:
       - If series index exists and series name (case/whitespace-insensitive)
         equals the book title, return "Book {index}".
       - If series index exists and series name differs from title, return
         "{series_name} #{index}".
       - If only series name, return it.
    3. Otherwise empty string.

    Index is coerced from int/float/str, dropping a trailing .0. Any unparseable
    index is treated as absent. All field access is defensive; never raises.
    """
    if not isinstance(book, dict):
        return ""

    subtitle = (book.get("subtitle") or "").strip()
    if subtitle:
        return subtitle

    series_name = (book.get("seriesName") or "").strip()
    if not series_name:
        return ""

    title = (book.get("title") or "").strip()

    # Coerce series index defensively
    raw_index = book.get("seriesIndex")
    index_str = None
    if raw_index is not None:
        try:
            if isinstance(raw_index, float):
                if raw_index.is_integer():
                    index_str = str(int(raw_index))
                else:
                    index_str = str(raw_index)
            elif isinstance(raw_index, int):
                index_str = str(raw_index)
            else:
                # str or other: try to parse as float then drop .0
                parsed = float(str(raw_index).strip())
                if parsed.is_integer():
                    index_str = str(int(parsed))
                else:
                    index_str = str(parsed)
        except (ValueError, TypeError):
            index_str = None

    if index_str:
        if series_name.lower() == title.lower():
            return f"Book {index_str}"
        return f"{series_name} #{index_str}"

    return series_name


def get_searchable_audiobooks(search_term):
    """Get audiobook results from all configured audio providers."""
    adapters = {}
    try:
        clients = uc()
        if clients.abs_client and clients.abs_client.is_configured():
            adapters["ABS"] = ABSAudioSourceAdapter(clients.abs_client)
        if clients.booklore_client and clients.booklore_client.is_configured():
            adapters["BookLore"] = BookLoreAudioSourceAdapter(clients.booklore_client, container.data_dir())
        _bo_client = getattr(clients, "bookorbit_client", None)
        if _bo_client and _bo_client.is_configured():
            adapters["BookOrbit"] = BookOrbitAudioSourceAdapter(_bo_client, container.data_dir())
    except Exception as e:
        logger.debug("Could not build user-scoped audio adapters, falling back to globals: %s", e)
        adapters = container.audio_source_adapters() if hasattr(container, "audio_source_adapters") else {}
    results = []
    seen = set()
    per_adapter_counts = {}

    for source_name, adapter in adapters.items():
        try:
            provider_results = adapter.search(search_term)
        except Exception as e:
            logger.warning(f"⚠️ Audiobook search failed for {source_name}: {e}", exc_info=True)
            per_adapter_counts[source_name] = f"error:{type(e).__name__}"
            continue
        per_adapter_counts[source_name] = len(provider_results) if provider_results else 0

        for result in provider_results or []:
            if not isinstance(result, AudioResult):
                continue
            key = (result.source, result.source_id)
            if key in seen:
                continue
            seen.add(key)
            results.append(result)

    results.sort(key=lambda item: (item.title or item.display_name or "").lower())
    logger.debug(
        "get_searchable_audiobooks(query=%r): adapters=%s, deduped_total=%d",
        search_term, per_adapter_counts, len(results),
    )
    return results


def _audiobook_search_variants(term):
    """Progressive query relaxations for a (possibly filename-derived) term.

    Yields the raw term, then with the file extension and trailing edition/year
    markers removed, then just the title before " - <author>". ABS title search
    is strict, so reviewing a suggestion whose title is a filename stem
    ("Title - Author (2026)") needs the bare title to match.
    """
    term = (term or "").strip()
    variants = []

    def _add(value):
        value = (value or "").strip()
        if value and value not in variants:
            variants.append(value)

    def _add_hyphen_space_variants(value):
        value = (value or "").strip()
        if not value:
            return

        if "-" in value:
            spaced = re.sub(r"\s*-\s*", " ", value).strip()
            _add(spaced)
            return

        words = re.split(r"\s+", value)
        if 2 <= len(words) <= 3:
            for index in range(len(words) - 1):
                hyphenated_words = words[:]
                hyphenated_words[index:index + 2] = [f"{words[index]}-{words[index + 1]}"]
                _add(" ".join(hyphenated_words))

    _add(term)
    no_ext = re.sub(r'\.(epub|pdf|mobi|azw3?|cbz|cbr|m4b|mp3)$', '', term, flags=re.IGNORECASE)
    no_year = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', no_ext).strip()
    no_edition = re.sub(
        r'\s*\((?:unabridged|abridged|audio(?:book)?|e-?book|kindle|retail|edition)\)\s*$',
        '',
        no_year,
        flags=re.IGNORECASE,
    ).strip()
    _add(no_year)
    _add(no_edition)
    if ' - ' in no_edition:
        title_part = no_edition.split(' - ')[0]
        _add(title_part)
        _add_hyphen_space_variants(title_part)
    else:
        _add_hyphen_space_variants(no_edition)
    return variants


def _search_audiobooks_with_fallback(term):
    """Search audiobooks, relaxing a filename-style term until something matches."""
    results = []
    for index, variant in enumerate(_audiobook_search_variants(term)):
        results = get_searchable_audiobooks(variant)
        if results:
            if index > 0:
                logger.debug("Audiobook search matched on relaxed term %r (from %r)", variant, term)
            break
    return results


def _ebook_is_provider(ebook):
    """True for a library-backed ebook (BookOrbit/Grimmory/ABS/CWA), not a bare file."""
    return bool(getattr(ebook, "source", None) and getattr(ebook, "source", None) != "Local File")


def _search_ebooks_with_fallback(term):
    """Search ebooks across the raw + relaxed terms and merge.

    The library providers (BookOrbit/Grimmory) use strict title search, so a
    filename-stem term ("Title - Author (2026)") only matches the local file. The
    relaxed title lets the provider match; results are deduped by filename with the
    provider entry preferred over the bare local file so the picker offers the
    library copy (whose progress actually syncs).
    """
    by_name = {}
    order = []
    for variant in _audiobook_search_variants(term):
        for ebook in get_searchable_ebooks(variant):
            key = (getattr(ebook, "name", "") or "").lower()
            if not key:
                continue
            existing = by_name.get(key)
            if existing is None:
                by_name[key] = ebook
                order.append(key)
            elif _ebook_is_provider(ebook) and not _ebook_is_provider(existing):
                by_name[key] = ebook  # upgrade a local-file hit to the library copy
    return [by_name[key] for key in order]


def get_suggestion_audiobooks():
    """Return provider-normalized audiobook records for suggestions scan."""
    records = []
    for item in get_searchable_audiobooks(""):
        if not isinstance(item, AudioResult):
            continue

        audio_source = (item.source or "").strip() or "ABS"
        source_id = str(item.source_id or "").strip()
        if not source_id:
            continue
        bridge_key = _build_bridge_key(audio_source, source_id)
        if not bridge_key:
            continue

        title = (item.title or item.display_name or bridge_key).strip()
        author = (item.authors or "").strip()
        cover_url = _browser_cover_url(
            item.cover_url,
            audio_source=audio_source,
            audio_source_id=source_id,
            abs_id=bridge_key,
        )
        records.append(
            {
                "bridge_key": bridge_key,
                "audio_source": audio_source,
                "audio_source_id": source_id,
                "audio_title": title,
                "audio_author": author,
                "audio_language": item.language or "",
                "audio_duration": item.duration,
                "audio_cover_url": cover_url,
                "audio_path": item.path or "",
                "audio_provider_book_id": str(item.provider_book_id or source_id),
                "audio_provider_file_id": str(item.provider_file_id or ""),
                # Legacy aliases maintained for compatibility with existing templates/session keys.
                "id": bridge_key,
                "title": title,
                "authors": author,
                "duration": item.duration,
                "cover_url": cover_url,
            }
        )

    return records


def _ebook_title_key(value):
    """Normalize a title/filename-stem to an alphanumeric key for joining."""
    return re.sub(r'[\W_]+', '', (value or '').lower())


def _build_local_ebook_title_index():
    """Map normalized title -> filename for every epub on the local /books disk.

    BookBridge and BookOrbit share the same files, so this lets us pair
    BookOrbit's clean metadata (title/author, no filename) with real filenames
    without a per-book BookOrbit detail call. Indexes both the full stem and the
    'Title' portion before ' - ' (filenames are 'Title - Author (year).epub')."""
    index = {}
    try:
        if EBOOK_DIR.exists():
            for eb in EBOOK_DIR.glob("**/*.epub"):
                stem = eb.stem
                title_part = stem.split(" - ", 1)[0]
                for key in (_ebook_title_key(title_part), _ebook_title_key(stem)):
                    if key:
                        index.setdefault(key, eb.name)
    except Exception as e:
        logger.warning(f"⚠️ Failed to build local ebook title index: {e}", exc_info=True)
    return index


def get_searchable_ebooks(search_term):
    """Get ebooks from Grimmory API, BookOrbit, filesystem, ABS, and CWA.
    Returns list of EbookResult objects for consistent interface."""

    results = []
    found_filenames = set()
    found_stems = set()  # To dedupe by title stem
    clients = uc()

    # 1. Grimmory
    if clients.booklore_client.is_configured():
        try:
            if search_term:
                books = clients.booklore_client.search_books(search_term)
            else:
                # For scan workloads, use the broader cache-oriented API to avoid
                # repeated aggressive refresh behavior from per-query search calls.
                books = clients.booklore_client.get_all_books()
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if fname.lower().endswith('.epub'):
                        found_filenames.add(fname.lower())
                        found_stems.add(Path(fname).stem.lower())
                        results.append(EbookResult(
                            name=fname,
                            title=b.get('title'),
                            subtitle=_ebook_edition_label(b),
                            authors=b.get('authors'),
                            language=b.get('language'),
                            booklore_id=b.get('id'),
                            path=b.get('filePath') or b.get('filepath') or b.get('path'),
                            source='Grimmory'
                        ))
        except Exception as e:
            logger.warning(f"⚠️ Grimmory search failed: {e}", exc_info=True)

    # 1b. BookOrbit
    if clients.bookorbit_client.is_configured():
        try:
            if search_term:
                # Targeted search returns real filenames (bounded result set).
                bo_books = clients.bookorbit_client.search_ebooks(search_term)
                local_index = None
            else:
                # Full scan: light candidates (clean title/author, no filename).
                # Pair each with a real filename from the shared /books disk so we
                # avoid a throttled detail call per book.
                bo_books = clients.bookorbit_client.get_all_ebooks()
                local_index = _build_local_ebook_title_index()
            for b in bo_books or []:
                fname = b.get('fileName') or ''
                if not fname and local_index is not None:
                    fname = local_index.get(_ebook_title_key(b.get('title'))) or ''
                if not fname.lower().endswith('.epub'):
                    continue
                if fname.lower() in found_filenames:
                    continue
                found_filenames.add(fname.lower())
                found_stems.add(Path(fname).stem.lower())
                results.append(EbookResult(
                    name=fname,
                    title=b.get('title'),
                    authors=b.get('authors'),
                    language=b.get('language'),
                    path=b.get('filePath') or b.get('filepath') or b.get('path'),
                    source='BookOrbit',
                    source_id=b.get('id'),
                    subtitle=_ebook_edition_label(b),
                ))
        except Exception as e:
            logger.warning(f"⚠️ BookOrbit search failed: {e}", exc_info=True)

    # 1c. BookFusion existing user library links. Search-only because BookFusion
    # does not provide a bridge-side EPUB download in Phase 1/3.
    if search_term and clients.bookfusion_client.is_configured():
        try:
            bf_books = clients.bookfusion_client.search_books(page=1, per_page=50, query=search_term)
            query_lower = search_term.lower()
            for b in bf_books or []:
                bf_id = b.get("id") or b.get("book_id")
                if bf_id in (None, ""):
                    continue
                title = str(b.get("title") or b.get("name") or f"BookFusion {bf_id}").strip()
                authors = _coerce_author_display(b.get("authors") or b.get("author"))
                haystack = f"{title} {authors}".lower()
                if query_lower and query_lower not in haystack:
                    continue
                fname = f"bookfusion_{bf_id}.epub"
                results.append(EbookResult(
                    name=fname,
                    title=title,
                    authors=authors,
                    language=b.get('language'),
                    path=None,
                    source='BookFusion',
                    source_id=bf_id,
                ))
        except Exception as e:
            logger.warning(f"⚠️ BookFusion search failed: {e}", exc_info=True)

    # 1d. Kavita
    kavita_client = getattr(clients, "kavita_client", None)
    if kavita_client and kavita_client.is_configured():
        try:
            kavita_books = (
                kavita_client.search_ebooks(search_term)
                if search_term
                else kavita_client.get_all_books()
            )
            for book in kavita_books or []:
                filename = book.get('fileName') or book.get('filename') or ''
                if not filename.lower().endswith('.epub'):
                    continue
                if filename.lower() in found_filenames:
                    continue
                found_filenames.add(filename.lower())
                found_stems.add(Path(filename).stem.lower())
                results.append(EbookResult(
                    name=filename,
                    title=book.get('title'),
                    subtitle=_ebook_edition_label(book),
                    authors=book.get('authors') or book.get('author'),
                    language=book.get('language'),
                    path=book.get('filePath') or book.get('path'),
                    source='Kavita',
                    source_id=book.get('id'),
                ))
        except Exception as e:
            logger.warning("Kavita search failed: %s", e, exc_info=True)

    # 2. ABS ebook libraries
    if search_term:
        try:
            abs_client = clients.abs_client
            if abs_client:
                abs_ebooks = abs_client.search_ebooks(search_term)
                if abs_ebooks:
                    for ab in abs_ebooks:
                        ebook_files = abs_client.get_ebook_files(ab['id'])
                        if ebook_files:
                            ef = ebook_files[0]
                            fname = f"{ab['id']}_abs.{ef['ext']}"
                            if fname.lower() not in found_filenames:
                                results.append(EbookResult(
                                    name=fname,
                                    title=ab.get('title'),
                                    authors=ab.get('author'),
                                    language=ab.get('language'),
                                    source='ABS',
                                    source_id=ab.get('id'),
                                    subtitle=_ebook_edition_label(ab)
                                ))
                                found_filenames.add(fname.lower())
                                if ab.get('title'):
                                    found_stems.add(ab['title'].lower().strip())
        except Exception as e:
            logger.warning(f"⚠️ ABS ebook search failed: {e}", exc_info=True)

    # 3. CWA (Calibre-Web Automated)
    if search_term:
        try:
            library_service = clients.library_service
            if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
                cwa_results = library_service.cwa_client.search_ebooks(search_term)
                if cwa_results:
                    try:
                        calibre_resolver = container.calibre_identifier_resolver()
                    except Exception:
                        calibre_resolver = None
                    resolver_enabled = bool(calibre_resolver and calibre_resolver.is_enabled())

                    for cr in cwa_results:
                        fname = f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}"
                        if fname.lower() not in found_filenames:
                            cwa_id = cr.get('id')
                            abs_identifier = None
                            if resolver_enabled and cwa_id:
                                try:
                                    abs_identifier = calibre_resolver.get_abs_id(cwa_id)
                                except Exception as e:
                                    logger.debug(f"Calibre identifier lookup failed for {cwa_id}: {e}")
                            results.append(EbookResult(
                                name=fname,
                                title=cr.get('title'),
                                authors=cr.get('author'),
                                language=cr.get('language'),
                                path=cr.get('download_url'),
                                source='CWA',
                                source_id=cwa_id,
                                abs_identifier=abs_identifier,
                            ))
                            found_filenames.add(fname.lower())
                            if cr.get('title'):
                                found_stems.add(cr['title'].lower().strip())
        except Exception as e:
            logger.warning(f"⚠️ CWA search failed: {e}", exc_info=True)

    # 4. Search filesystem (Local) - LOW PRIORITY
    if EBOOK_DIR.exists():
        try:
            all_epubs = list(EBOOK_DIR.glob("**/*.epub"))
            for eb in all_epubs:
                fname_lower = eb.name.lower()
                stem_lower = eb.stem.lower()

                # Dedupe: if already found in rich source, skip
                if fname_lower in found_filenames or stem_lower in found_stems:
                    continue

                if not search_term or search_term.lower() in fname_lower:
                    results.append(EbookResult(name=eb.name, path=eb, source='Local File'))
                    found_filenames.add(fname_lower)
                    found_stems.add(stem_lower)

        except Exception as e:
            logger.warning(f"⚠️ Filesystem search failed: {e}", exc_info=True)

    # Check if we have no sources at all
    if (not results and not EBOOK_DIR.exists()
            and not clients.booklore_client.is_configured()
            and not clients.bookorbit_client.is_configured()
            and not _optional_client_configured(clients, "kavita_client")
            and not clients.bookfusion_client.is_configured()):
        get_persistent_condition_logger().warn(
            logger,
            "no_ebook_source_configured",
            "⚠️ No ebooks available: No ebook source configured. "
            "Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD), "
            "enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, BOOKORBIT_PASSWORD), "
            "enable Kavita (KAVITA_SERVER, KAVITA_API_KEY), "
            "link BookFusion, "
            "or mount the ebooks directory to /books"
        )

    return results


def _promote_authoritative_ebook_matches(audiobooks, ebooks):
    """Stable-sort ebooks so any whose abs_identifier matches an audiobook source_id rises to the top."""
    if not ebooks or not audiobooks:
        return ebooks
    ab_ids = set()
    for ab in audiobooks:
        sid = getattr(ab, 'source_id', None)
        if sid is not None:
            sid_str = str(sid).strip()
            if sid_str:
                ab_ids.add(sid_str)
    if not ab_ids:
        return ebooks

    def _key(eb):
        ident = getattr(eb, 'abs_identifier', None)
        if ident and str(ident).strip() in ab_ids:
            return 0
        return 1

    ebooks.sort(key=_key)
    return ebooks


# Non-ABS audiobook providers. Their mappings live under a prefixed bridge key
# ('booklore:<id>' / 'bookorbit:<id>') instead of an ABS item id.
_LIBRARY_AUDIO_SOURCES = ("BookLore", "BookOrbit")
_AUDIO_BRIDGE_PREFIXES = {"booklore": "booklore", "bookorbit": "bookorbit"}


def _audio_source_display_name(source):
    """'BookLore' is the internal key; users know it as Grimmory."""
    return "Grimmory" if source == "BookLore" else str(source or "")


def _audio_source_from_bridge_key(bridge_key):
    """Infer the audio source from a bridge key's prefix ('booklore:'/'bookorbit:').

    Un-prefixed non-empty keys are ABS item ids; an empty key yields ''."""
    key = str(bridge_key or "")
    for src in _LIBRARY_AUDIO_SOURCES:
        if key.lower().startswith(f"{src.lower()}:"):
            return src
    return "ABS" if key else ""


# Map internal audio source key -> (UI badge label, CSS class).
# These are the FULL product names used on UI chips, deliberately distinct from
# _audio_source_display_name(), which stays short ("ABS") because it feeds
# generated book titles.
_AUDIO_SOURCE_BADGE_LABELS = {
    "ABS": ("Audiobookshelf", "abs"),
    "BookLore": ("Grimmory", "grimmory"),
    "BookOrbit": ("BookOrbit", "bookorbit"),
}


def suggestion_source_badge(audio_source: str | None, bridge_key: str | None = None) -> tuple[str, str]:
    """Resolve a suggestion's audio provider to a (label, css_class) badge pair."""
    source = (audio_source or _audio_source_from_bridge_key(bridge_key) or "ABS").strip()
    return _AUDIO_SOURCE_BADGE_LABELS.get(source, (source, "unknown"))


def _build_bridge_key(audio_source, audio_source_id):
    if audio_source_id is None:
        return None
    source_id = str(audio_source_id).strip()
    if not source_id:
        return None

    head = source_id.lower().split(":", 1)[0]
    if ":" in source_id and head in _AUDIO_BRIDGE_PREFIXES:
        return f"{head}:{source_id.split(':', 1)[1].strip()}"

    source_name = str(audio_source or "").strip().lower()
    if source_name in _AUDIO_BRIDGE_PREFIXES:
        return f"{_AUDIO_BRIDGE_PREFIXES[source_name]}:{source_id}"
    return source_id


def _normalize_text_source_type(raw_source):
    return normalize_ebook_source(raw_source)


def _safe_local_source_path(raw_path) -> str:
    """Return a local ebook path only when it resolves inside a library root.

    The value arrives in a request payload, so it is confined to BOOKS_DIR /
    EXTRA_EBOOK_DIRS / the epub cache before any staging, parsing, hashing, or
    upload reads it. Returns '' for a missing or out-of-tree path, which the
    forge callers already treat as "local file path unavailable".
    """
    raw = str(raw_path or "").strip()
    if not raw:
        return ""
    safe_path = safe_library_path(raw)
    if safe_path is None:
        logger.warning(
            "Refused local ebook source outside the configured library roots: %s",
            sanitize_log_data(raw),
        )
        return ""
    return str(safe_path)


def _build_forge_text_item(source_type, source_id, source_path, original_filename):
    normalized_source = _normalize_text_source_type(source_type)
    normalized_source_id = str(source_id or "").strip()
    normalized_source_path = str(source_path or "").strip()

    text_item = {
        "source": normalized_source,
        "path": normalized_source_path,
        "booklore_id": normalized_source_id,
        "bookorbit_id": normalized_source_id,
        "kavita_id": normalized_source_id,
        "bookfusion_id": normalized_source_id,
        "cwa_id": normalized_source_id,
        "abs_id": normalized_source_id,
        "source_id": normalized_source_id,
        "filename": original_filename,
    }

    if normalized_source == "ABS":
        text_item["abs_id"] = normalized_source_id
    if normalized_source == "Booklore":
        text_item["booklore_id"] = normalized_source_id
    if normalized_source == "BookOrbit":
        text_item["bookorbit_id"] = normalized_source_id
    if normalized_source == "Kavita":
        text_item["kavita_id"] = normalized_source_id
    if normalized_source == "BookFusion":
        text_item["bookfusion_id"] = normalized_source_id
    if normalized_source == "CWA":
        text_item["cwa_id"] = normalized_source_id
        if normalized_source_path:
            text_item["download_url"] = normalized_source_path
    if normalized_source == "Local File":
        text_item["path"] = _safe_local_source_path(normalized_source_path)

    return text_item


def _parse_audio_duration(raw_value):
    try:
        if raw_value is None or raw_value == "":
            return None
        return float(raw_value)
    except (TypeError, ValueError):
        return None


def _create_or_update_library_audio_mapping(
    *,
    audio_source="BookLore",
    audio_source_id,
    audio_title,
    audio_cover_url,
    audio_duration,
    audio_provider_book_id,
    audio_provider_file_id,
    ebook_filename,
    ebook_source,
    ebook_source_id,
    storyteller_uuid,
    ebook_source_path=None,
):
    """Create/refresh a mapping whose audio lives on a library provider
    (Grimmory or BookOrbit) instead of ABS."""
    if audio_source not in _LIBRARY_AUDIO_SOURCES:
        return None, f"Unsupported audio source '{audio_source}'", 400
    audio_display = _audio_source_display_name(audio_source)
    bridge_key = _build_bridge_key(audio_source, audio_source_id)
    existing_book = (
        database_service.get_book(bridge_key)
        or database_service.get_book_by_audio_source(audio_source, audio_source_id)
    )

    resolved_ebook_filename = (ebook_filename or "").strip() or None
    original_ebook_filename = resolved_ebook_filename
    if existing_book and not original_ebook_filename:
        original_ebook_filename = existing_book.original_ebook_filename

    if storyteller_uuid:
        artifact_filename, _artifact_path = _download_storyteller_artifact(
            storyteller_uuid,
            audio_title,
            original_ebook_filename=original_ebook_filename,
        )
        if not artifact_filename:
            return None, "Failed to download Storyteller artifact", 500
        resolved_ebook_filename = artifact_filename

    if not resolved_ebook_filename:
        return None, "Please select a text source (Storyteller or Standard Ebook)", 400

    booklore_ebook_id = None
    if is_grimmory_source(ebook_source):
        booklore_ebook_id = ebook_source_id
    elif uc().booklore_client.is_configured():
        bl_book = uc().booklore_client.find_book_by_filename(original_ebook_filename or resolved_ebook_filename)
        if bl_book:
            booklore_ebook_id = bl_book.get("id")

    if storyteller_uuid:
        kosync_doc_id = _compute_storyteller_trilink_kosync_id(
            original_ebook_filename,
            resolved_ebook_filename,
            f"{audio_display} audiobook match",
        )
    else:
        kosync_doc_id = get_kosync_id_for_ebook(
            resolved_ebook_filename,
            booklore_ebook_id,
            bookorbit_id=ebook_source_id if ebook_source == "BookOrbit" else None,
            kavita_id=ebook_source_id if ebook_source == "Kavita" else None,
            source_path=ebook_source_path,
        )

    if existing_book and existing_book.kosync_doc_id:
        kosync_doc_id = existing_book.kosync_doc_id

    if not kosync_doc_id:
        return None, "Could not compute KOSync ID for ebook", 404

    from src.db.models import Book

    default_cover = (
        f"/api/bookorbit/audiobook-cover/{audio_source_id}"
        if audio_source == "BookOrbit"
        else f"/api/booklore/audiobook-cover/{audio_source_id}"
    )
    # An existing mapping keeps the id it was stored under. `get_book_by_audio_source`
    # can return a row whose abs_id is not the bridge key, and re-keying it here would
    # strand the states, annotations and per-user claims filed against the old id.
    # Consolidating a genuine duplicate is `absorb_duplicate_mapping`'s job, below.
    target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook")
    target_book.abs_title = audio_title or target_book.abs_title or bridge_key
    _preserve_or_reset_mapping_status(
        target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
    )
    target_book.audio_source = audio_source
    target_book.audio_source_id = str(audio_source_id)
    target_book.audio_title = audio_title or target_book.audio_title or target_book.abs_title
    target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or default_cover
    target_book.audio_duration = audio_duration if audio_duration is not None else target_book.audio_duration
    target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
    target_book.audio_provider_file_id = str(audio_provider_file_id) if audio_provider_file_id else target_book.audio_provider_file_id
    target_book.ebook_filename = resolved_ebook_filename
    target_book.original_ebook_filename = original_ebook_filename or target_book.original_ebook_filename
    target_book.ebook_source = ebook_source or target_book.ebook_source
    target_book.ebook_source_id = ebook_source_id or target_book.ebook_source_id
    target_book.kosync_doc_id = kosync_doc_id
    target_book.sync_mode = "audiobook"
    target_book.duration = audio_duration if audio_duration is not None else target_book.duration
    target_book.storyteller_uuid = storyteller_uuid or target_book.storyteller_uuid
    target_book.transcript_file = existing_book.transcript_file if existing_book else None
    target_book.transcript_source = existing_book.transcript_source if existing_book else None

    if storyteller_uuid:
        storyteller_manifest = ingest_storyteller_transcripts(
            target_book.abs_id,
            target_book.abs_title or "",
            [],
        )
        target_book.transcript_file = storyteller_manifest
        target_book.transcript_source = _storyteller_transcript_source(
            storyteller_uuid,
            storyteller_manifest,
        )

    # Resolve series from the owning library so the mapping collapses into its series
    # card immediately. The other match paths already do this (ABS metadata; the
    # shelf-watch / book_mapping_service path), but the library-audio path (BookOrbit/
    # Grimmory) did not, so a freshly matched book had no series until a manual
    # backfill. One detail call per match; matches are infrequent.
    if not target_book.series_name:
        try:
            resolution = resolve_series_details(
                target_book,
                abs_client=uc().abs_client,
                bookorbit_client=uc().bookorbit_client,
                booklore_client=uc().booklore_client,
                kavita_client=uc().kavita_client,
                ebook_parser=container.ebook_parser(),
            )
            if resolution.name:
                target_book.series_name = resolution.name
                target_book.series_sequence = resolution.sequence
        except Exception as series_err:
            logger.warning(
                "Series resolve on match failed for '%s': %s",
                sanitize_log_data(target_book.abs_title), series_err, exc_info=True,
            )

    saved_book = database_service.save_book(target_book)

    # An ebook-only mapping for this same ebook may already exist -- adding an
    # audiobook to a book you already had as an ebook is exactly this path. Without
    # this, both rows survive and only one can own the KOSync document hash, so the
    # loser is served to devices but can never receive progress.
    database_service.absorb_duplicate_mapping(saved_book)

    if uc().storyteller_client.is_configured() and saved_book.storyteller_uuid:
        try:
            uc().storyteller_client.add_to_collection_by_uuid(saved_book.storyteller_uuid)
        except Exception as st_err:
            logger.warning(f"Failed to add Storyteller UUID to collection: {st_err}", exc_info=True)

    shelf_filename = saved_book.original_ebook_filename or saved_book.ebook_filename
    if shelf_filename and not _is_storyteller_artifact_filename(shelf_filename):
        try:
            _shelve_matched_ebook(
                shelf_filename,
                ebook_source=saved_book.ebook_source,
                ebook_source_id=saved_book.ebook_source_id,
            )
        except Exception as shelf_err:
            logger.warning(f"Failed to shelve matched ebook '{shelf_filename}': {shelf_err}", exc_info=True)

    database_service.dismiss_suggestion(saved_book.abs_id)
    if isinstance(saved_book.kosync_doc_id, str) and saved_book.kosync_doc_id.strip():
        database_service.dismiss_suggestion(saved_book.kosync_doc_id)
    _adopt_kosync_progress_for_book(saved_book.abs_id, saved_book.kosync_doc_id)

    return saved_book, None, None


def _create_or_update_audio_only_mapping(
    *,
    audio_source="ABS",
    audio_source_id,
    audio_title=None,
    audio_cover_url=None,
    audio_duration=None,
    audio_provider_book_id=None,
    audio_provider_file_id=None,
):
    """Create or refresh a mapping that contains only an audiobook source.

    Audio-only mappings intentionally have no EPUB, KOSync hash, transcript, or
    alignment work.  They still use the normal provider bridge keys so ABS,
    Grimmory, and BookOrbit audio progress can be tracked consistently.
    """
    audio_source = str(audio_source or "").strip()
    audio_source_id = str(audio_source_id or "").strip()
    if audio_source not in ("ABS", *_LIBRARY_AUDIO_SOURCES):
        return None, f"Unsupported audio source '{audio_source}'", 400
    if not audio_source_id:
        return None, "Missing audiobook source id", 400

    bridge_key = (
        audio_source_id
        if audio_source == "ABS"
        else _build_bridge_key(audio_source, audio_source_id)
    )
    existing_book = (
        database_service.get_book(bridge_key)
        or database_service.get_book_by_audio_source(audio_source, audio_source_id)
    )

    clients = uc()
    default_cover = None
    if audio_source == "ABS":
        default_cover = f"/api/cover-proxy/{audio_source_id}"
    elif audio_source == "BookLore":
        default_cover = f"/api/booklore/audiobook-cover/{audio_source_id}"
    else:
        default_cover = f"/api/bookorbit/audiobook-cover/{audio_source_id}"

    from src.db.models import Book

    title = audio_title or f"{_audio_source_display_name(audio_source)} {audio_source_id}"
    target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook_only")
    target_book.abs_id = bridge_key
    target_book.abs_title = title or target_book.abs_title or bridge_key
    target_book.audio_source = audio_source
    target_book.audio_source_id = audio_source_id
    target_book.audio_title = title or target_book.audio_title or target_book.abs_title
    target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or default_cover
    if audio_duration is not None:
        target_book.audio_duration = audio_duration
        target_book.duration = audio_duration
    target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
    target_book.audio_provider_file_id = (
        str(audio_provider_file_id) if audio_provider_file_id else None
    )

    # A deliberate audio-only rematch must not leave stale text-side metadata
    # visible or active in the mapping.
    for field_name in (
        "ebook_filename",
        "ebook_source",
        "ebook_source_id",
        "original_ebook_filename",
        "kosync_doc_id",
        "transcript_file",
        "transcript_source",
        "storyteller_uuid",
        "bookfusion_id",
        "abs_ebook_item_id",
    ):
        setattr(target_book, field_name, None)
    target_book.status = "active"
    target_book.sync_mode = "audiobook_only"

    saved_book = database_service.save_book(target_book)

    if audio_source == "ABS":
        try:
            clients.abs_client.add_to_collection(
                saved_book.abs_id,
                user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"),
            )
        except Exception as exc:
            logger.warning("Failed to add audio-only ABS mapping to collection: %s", exc, exc_info=True)

    try:
        database_service.dismiss_suggestion(saved_book.abs_id)
    except Exception:
        pass

    return saved_book, None, None


def _ensure_bookfusion_ebook_cached(bookfusion_id) -> "str | None":
    """Download a BookFusion EPUB into the epub cache and return its filename.

    Makes BookFusion behave like every other ebook source: the file is written to
    the epub cache as ``bookfusion_<id>.epub`` so it resolves for KOSync hashing,
    progress text-anchoring, and annotation offset math. Returns ``None`` (best
    effort) when BookFusion is unconfigured or the download/write fails.
    """
    bf_id = str(bookfusion_id or "").strip()
    if not bf_id:
        return None
    filename = f"bookfusion_{bf_id}.epub"
    epub_cache = container.epub_cache_dir()
    epub_cache.mkdir(parents=True, exist_ok=True)
    cached_path = epub_cache / filename
    try:
        if cached_path.exists() and cached_path.stat().st_size > 0:
            return filename
    except OSError:
        pass

    client = uc().bookfusion_client
    if not client.is_configured():
        return None
    try:
        content = client.download_book(bf_id)
    except Exception as e:
        logger.warning("⚠️ BookFusion download failed for '%s': %s", bf_id, e, exc_info=True)
        return None
    if not content:
        logger.warning("⚠️ BookFusion download returned no content for '%s'", bf_id)
        return None
    try:
        cached_path.write_bytes(content)
    except Exception as e:
        logger.error("❌ Could not cache BookFusion EPUB for '%s': %s", bf_id, e, exc_info=True)
        return None
    logger.info("📥 Cached BookFusion EPUB '%s' (%d bytes)", filename, len(content))
    return filename


def _link_bookfusion_ebook_source(target_book, bookfusion_id) -> None:
    """Attach a downloaded BookFusion EPUB to a book so it syncs like other ebook
    sources: set the ebook fields and compute the KOSync hash for KOReader linking.

    Best effort — if the file can't be acquired, the book keeps its BookFusion
    progress link but annotations/leading progress stay unavailable until the EPUB
    is cached. The KOSync document is linked by the caller after ``save_book``.
    """
    ebook_filename = _ensure_bookfusion_ebook_cached(bookfusion_id)
    if not ebook_filename:
        logger.warning(
            "⚠️ BookFusion '%s' linked without a cached EPUB; progress is one-way and "
            "annotations will not sync until the file can be downloaded.",
            bookfusion_id,
        )
        return
    target_book.ebook_filename = ebook_filename
    target_book.original_ebook_filename = ebook_filename
    target_book.ebook_source = "BookFusion"
    target_book.ebook_source_id = str(bookfusion_id)
    try:
        cached_path = container.epub_cache_dir() / ebook_filename
        kosync_id = container.ebook_parser().get_kosync_id(cached_path)
    except Exception as e:
        logger.warning("⚠️ Could not compute KOSync hash for BookFusion '%s': %s", bookfusion_id, e, exc_info=True)
        kosync_id = None
    if kosync_id:
        target_book.kosync_doc_id = kosync_id


def _create_or_update_bookfusion_progress_mapping(
    *,
    audio_source="ABS",
    audio_source_id,
    audio_title,
    audio_cover_url=None,
    audio_duration=None,
    audio_provider_book_id=None,
    audio_provider_file_id=None,
    bookfusion_id,
    bookfusion_title=None,
    bookfusion_author=None,
    storyteller_uuid=None,
):
    """Create/update an audio mapping linked to a BookFusion ebook.

    Downloads + caches BookFusion's EPUB and hashes it for KOReader linking, sets
    status 'pending' so the forge/Whisper pipeline aligns audio↔ebook, tri-links a
    Storyteller readalong when ``storyteller_uuid`` is given (BookFusion's own EPUB
    stays as ``original_ebook_filename`` for annotation offsets), persists the
    per-user BookFusion link, and enqueues Hardcover/StoryGraph auto-match — i.e. it
    behaves like every other ebook source instead of a progress-only dead end.
    """
    if not bookfusion_id:
        return None, "Missing BookFusion book id", 400
    if audio_source not in ("ABS", *_LIBRARY_AUDIO_SOURCES):
        return None, f"Unsupported audio source '{audio_source}'", 400
    if not audio_source_id:
        return None, "BookFusion linking requires an audio source", 400

    from src.db.models import Book

    if audio_source in _LIBRARY_AUDIO_SOURCES:
        abs_id = _build_bridge_key(audio_source, audio_source_id)
        existing_book = (
            database_service.get_book(abs_id)
            or database_service.get_book_by_audio_source(audio_source, audio_source_id)
        )
        default_cover = (
            f"/api/bookorbit/audiobook-cover/{audio_source_id}"
            if audio_source == "BookOrbit"
            else f"/api/booklore/audiobook-cover/{audio_source_id}"
        )
        resolved_title = audio_title or bookfusion_title or f"{_audio_source_display_name(audio_source)} {audio_source_id}"
    else:
        abs_id = str(audio_source_id)
        existing_book = database_service.get_book(abs_id)
        default_cover = audio_cover_url
        resolved_title = audio_title or bookfusion_title or abs_id

    target_book = existing_book or Book(abs_id=abs_id, sync_mode="audiobook")
    _preserve_or_reset_mapping_status(
        target_book,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
    )
    target_book.abs_id = abs_id
    target_book.abs_title = resolved_title or target_book.abs_title or abs_id
    target_book.audio_source = audio_source
    target_book.audio_source_id = str(audio_source_id)
    target_book.audio_title = resolved_title or target_book.audio_title
    target_book.audio_cover_url = audio_cover_url or target_book.audio_cover_url or default_cover
    target_book.audio_duration = audio_duration if audio_duration is not None else target_book.audio_duration
    target_book.audio_provider_book_id = str(audio_provider_book_id or audio_source_id)
    target_book.audio_provider_file_id = str(audio_provider_file_id) if audio_provider_file_id else target_book.audio_provider_file_id
    target_book.duration = audio_duration if audio_duration is not None else target_book.duration
    target_book.sync_mode = "audiobook"

    # Acquire the EPUB locally so BookFusion behaves like every other ebook source
    # (cached file + KOSync hash → progress text-anchoring and annotation offsets).
    _link_bookfusion_ebook_source(target_book, bookfusion_id)

    # Storyteller tri-link: the readalong artifact becomes the working text file
    # (drives forge/Whisper alignment) while BookFusion's own EPUB stays as
    # original_ebook_filename so annotation offsets resolve against it — mirrors the
    # Grimmory/BookOrbit + Storyteller tri-link model.
    if storyteller_uuid:
        bf_original = target_book.ebook_filename
        artifact_filename, _artifact_path = _download_storyteller_artifact(
            storyteller_uuid, target_book.abs_title, original_ebook_filename=bf_original,
        )
        if artifact_filename:
            target_book.storyteller_uuid = storyteller_uuid
            if bf_original:
                target_book.original_ebook_filename = bf_original
            target_book.ebook_filename = artifact_filename
            trilink_hash = _compute_storyteller_trilink_kosync_id(
                bf_original, artifact_filename, "BookFusion Tri-Link",
            )
            if trilink_hash:
                target_book.kosync_doc_id = trilink_hash
            chapters = _resolve_abs_chapters_for_storyteller_ingest(target_book)
            manifest = ingest_storyteller_transcripts(
                target_book.abs_id, target_book.abs_title or "", chapters,
            )
            target_book.transcript_file = manifest
            target_book.transcript_source = _storyteller_transcript_source(storyteller_uuid, manifest)
        else:
            logger.warning(
                "⚠️ BookFusion tri-link: could not obtain Storyteller artifact '%s' for '%s'",
                storyteller_uuid, target_book.abs_title,
            )

    saved_book = database_service.save_book(target_book)
    if not isinstance(getattr(saved_book, "abs_id", None), str):
        saved_book = target_book

    if getattr(saved_book, "kosync_doc_id", None):
        # Fold in any older mapping for this same ebook before claiming the hash
        # below -- two rows can each carry the hash, but only one can be the book
        # a device's progress resolves to.
        database_service.absorb_duplicate_mapping(saved_book)
        try:
            database_service.ensure_linked_kosync_document(saved_book.kosync_doc_id, saved_book.abs_id)
        except Exception as e:
            logger.warning(
                "⚠️ Could not link BookFusion KOSync document for '%s': %s", saved_book.abs_id, e, exc_info=True
            )

    _persist_bookfusion_link_for_current_user(
        saved_book.abs_id,
        "BookFusion",
        str(bookfusion_id),
        title=bookfusion_title or target_book.abs_title,
        author=bookfusion_author,
    )
    try:
        _enqueue_tracker_automatch(uc().sync_clients, saved_book)
    except Exception as e:
        logger.warning(
            "⚠️ Could not enqueue tracker automatch for BookFusion book '%s': %s",
            saved_book.abs_id, e, exc_info=True,
        )
    database_service.dismiss_suggestion(saved_book.abs_id)
    return saved_book, None, None



def restart_server():
    """
    Triggers a graceful restart by sending SIGTERM to the current process.
    The start.sh supervisor loop will catch the exit and restart the application.
    """
    logger.info("♻️  Stopping application (Supervisor will restart it)...")
    time.sleep(1.0)  # Give Flask time to send the redirect response

    # Send SIGTERM to our own process so the main thread's signal handler fires.
    # Note: sys.exit() does NOT work here because this runs in a background thread —
    # sys.exit() only raises SystemExit in the calling thread, not the main process.
    logger.info("👋 Sending SIGTERM to trigger restart...")
    import signal
    os.kill(os.getpid(), signal.SIGTERM)

def start_restart_async():
    threading.Thread(target=restart_server, daemon=True).start()

def render_restarting_page(next_url, health_url, restart_url):
    return render_template_string(
        RESTARTING_PAGE_TEMPLATE,
        next_url=next_url,
        health_url=health_url,
        restart_url=restart_url,
    )

def api_health():
    """Lightweight readiness endpoint for restart polling."""
    response = jsonify({
        "ok": True,
        "version": APP_VERSION,
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

def api_restart():
    """Trigger an asynchronous app restart after the restart page has loaded."""
    start_restart_async()
    response = jsonify({"ok": True})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

def settings():
    # Application Defaults
    # Note: These are also defined in inject_global_vars for context processor usage
    # We should probably centralize them, but for now this works.

    if request.method == 'POST':
        # User-management actions from the Settings → Users tab post here too.
        # Handle them and bounce back to the Users tab (no settings save / restart).
        if request.form.get('action') in _USER_ADMIN_ACTIONS:
            u_message, u_error = _apply_user_admin_action(request.form)
            session['user_message'] = u_message
            session['user_error'] = u_error
            return redirect(url_for('settings') + '#users')

        bool_keys = [
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'KOSYNC_AUTO_MAP_ON_AGREEMENT',
            'KOSYNC_HASH_RECONCILE_ENABLED',
            'KOSYNC_XPATH_ORDER_ENABLED',
            'KOREADER_ANNOTATION_SYNC',
            'SYNC_FRESHNESS_GUARDS',
            'SYNC_TRUST_CORROBORATED_REWIND',
            'SYNC_COMPLETION_PROPAGATION',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'ABS_ENABLED',
            'READEST_ENABLED',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'BOOKFUSION_ENABLED',
            'GRIMMORY_READING_SESSIONS',
            'CWA_ENABLED',
            'CWA_SYNC_ENABLED',
            'CWA_KOBO_SPAN_SYNC',
            'HARDCOVER_ENABLED',
            'HARDCOVER_ANNOTATION_SYNC',
            'STORYGRAPH_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'SUGGESTIONS_AUTO_MATCH_ENABLED',
            'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'STORYTELLER_POLL_WAIT_FOR_SETTLE',
            'BOOKFUSION_POLL_WAIT_FOR_SETTLE',
            'BOOKORBIT_AUDIO_POLL_WAIT_FOR_SETTLE',
            'BOOKLORE_AUDIO_POLL_WAIT_FOR_SETTLE',
            'BOOKORBIT_POLL_WAIT_FOR_SETTLE',
            'BOOKLORE_POLL_WAIT_FOR_SETTLE',
            'KAVITA_POLL_WAIT_FOR_SETTLE',
            'CWA_SYNC_POLL_WAIT_FOR_SETTLE',
            'STORYTELLER_LISTENING_SESSIONS',
            'STORYTELLER_NO_EPUB_CACHE',
            'BOOKLORE_SHELF_WATCH_ENABLED',
            'BOOKORBIT_ENABLED',
            'BOOKORBIT_READING_SESSIONS',
            'BOOKORBIT_SHELF_WATCH_ENABLED',
            'KAVITA_ENABLED',
            'KAVITA_SHELF_WATCH_ENABLED',
            'CALIBRE_USE_ABS_IDENTIFIER',
            'SHELFMARK_ENABLED',
            'OLLAMA_ENABLED',
            'OLLAMA_RERANK_SUGGESTIONS',
            'OLLAMA_JUDGE_SUGGESTIONS',
            'OLLAMA_ALIGN_FALLBACK',
            'OLLAMA_ALIGN_ANCHOR_RESCUE',
            'OLLAMA_ALIGN_CONTENT_GUARD',
            'OLLAMA_SUGGEST_JUDGE_GATE',
            'OLLAMA_TRACKER_MATCH',
            'OLLAMA_LIBRARY_MATCH',
            'OLLAMA_EBOOK_TEXT_FALLBACK',
            'DIAGNOSTICS_OPT_IN',
            'WHISPER_CPP_SEND_ORIGINAL',
            'CTC_ENABLED',
            'ALIGNMENT_SEGMENTED_MAPS',
            'CONTENT_MATCH_GUARD',
            'SHARE_ALL_BOOKS_WITH_ALL_USERS',
            'REMOTE_AUTH_ENABLED',
        ]

        # Current settings in DB
        current_settings = database_service.get_all_settings()
        booklore_setting_keys = [
            'BOOKLORE_LIBRARY_ID',
            'BOOKLORE_SERVER',
            'BOOKLORE_USER',
            'BOOKLORE_PASSWORD',
        ]
        old_booklore_settings = {
            key: (current_settings.get(key) or '').strip()
            for key in booklore_setting_keys
        }
        url_keys = [
            'SHELFMARK_URL', 'ABS_SERVER', 'ABS_WEB_URL', 'BOOKLORE_SERVER', 'BOOKLORE_WEB_URL',
            'BOOKORBIT_WEB_URL', 'CWA_WEB_URL', 'BOOKFUSION_API_URL',
            'KAVITA_SERVER', 'KAVITA_WEB_URL',
            'STORYTELLER_API_URL', 'CWA_SERVER', 'KOSYNC_SERVER',
            'OLLAMA_URL', 'LLM_BASE_URL',
        ]

        def _normalized_form_value(key):
            if key in request.form:
                raw_value = request.form.get(key, '')
            else:
                raw_value = current_settings.get(key, '')

            clean_value = _normalize_abs_form_value(key, raw_value)
            if key in url_keys and clean_value and key != "ABS_SERVER":
                lower_val = clean_value.lower()
                if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
                    clean_value = f"http://{clean_value}"
            return clean_value

        # 1. Handle Boolean Toggles (Checkbox logic)
        # Checkboxes are NOT sent if unchecked, so we must check every known bool key
        for key in bool_keys:
            is_checked = (key in request.form)
            # Save "true" or "false"
            val_str = str(is_checked).lower()
            database_service.set_setting(key, val_str)
            os.environ[key] = val_str # Immediate update for current process

        # 2. Handle Text Inputs
        # Iterate over form to find other keys
        for key, value in request.form.items():
            if key in bool_keys: continue

            # Only recognized settings are persisted. The posted form also carries
            # control fields — csrf_token, injected into every form by the CSRF
            # bootstrap script — and this loop used to write each of them to the
            # settings table as if it were configuration.
            if key not in KNOWN_SETTING_KEYS:
                if key.isupper():
                    # Looks like a setting but is registered nowhere: almost always
                    # a new field added to the template without ALL_SETTINGS.
                    logger.warning(
                        "⚠️ Settings save: ignoring unregistered key '%s' — add it to "
                        "ALL_SETTINGS/DEFAULT_CONFIG in config_loader.py to make it savable",
                        sanitize_log_data(key),
                    )
                else:
                    logger.debug("Settings save: ignoring non-setting form field '%s'", key)
                continue

            clean_value = _normalize_abs_form_value(key, value)

            # Sanitize URLs
            if key in url_keys and clean_value and key != "ABS_SERVER":
                lower_val = clean_value.lower()
                if not (lower_val.startswith("http://") or lower_val.startswith("https://")):
                    clean_value = f"http://{clean_value}"

            if clean_value:
                database_service.set_setting(key, clean_value)
                os.environ[key] = clean_value # Immediate update for current process
            elif key in current_settings:
                database_service.set_setting(key, "")
                os.environ[key] = "" # Immediate update for current process

        new_booklore_settings = {
            key: _normalized_form_value(key)
            for key in booklore_setting_keys
        }
        if any(old_booklore_settings[key] != new_booklore_settings[key] for key in booklore_setting_keys):
            logger.info("Grimmory settings changed; clearing Grimmory cache before restart")
            database_service.clear_all_booklore_books()
            client = container.booklore_client()
            with client._cache_lock:
                client._book_cache.clear()
                client._book_id_cache.clear()
                client._cache_timestamp = 0

        try:
            return render_restarting_page(
                next_url=url_for('index'),
                health_url=url_for('api_health'),
                restart_url=url_for('api_restart'),
            )
        except Exception as e:
            session['message'] = f"Error saving settings: {e}"
            session['is_error'] = True
            logger.error(f"❌ Error saving settings: {e}", exc_info=True)

        return redirect(url_for('settings'))

    # GET Request
    message = session.pop('message', None)
    is_error = session.pop('is_error', False)
    user_message = session.pop('user_message', None)
    user_error = session.pop('user_error', None)
    try:
        users = list(database_service.list_users())
    except Exception:
        users = []
    cu = current_user()
    try:
        primary_admin_id = database_service._default_user_id()
    except Exception:
        primary_admin_id = None

    response = make_response(render_template('settings.html',
                         message=message,
                         is_error=is_error,
                         users=users,
                         current_user_id=(cu.id if cu else None),
                         primary_admin_id=primary_admin_id,
                         user_message=user_message,
                         user_error=user_error))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response

def get_abs_author(ab):

    """Extract author from ABS audiobook metadata."""
    media = ab.get('media', {})
    metadata = media.get('metadata', {})
    return metadata.get('authorName') or (metadata.get('authors') or [{}])[0].get("name", "")


def _coerce_author_display(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return (value.get("name") or value.get("authorName") or "").strip()
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                name = (item.get("name") or item.get("authorName") or "").strip()
            else:
                name = str(item).strip() if item is not None else ""
            if name:
                names.append(name)
        return ", ".join(names)
    return ""


def _extract_series_from_abs_metadata(metadata: dict) -> tuple:
    """Return (series_name, series_sequence) from an ABS media.metadata block."""
    return _series_from_abs_metadata(metadata)


def _extract_series_from_booklore_metadata(raw: dict) -> tuple:
    """Return (series_name, series_sequence) from cached BookLore raw_metadata."""
    return _series_from_library_detail(raw)


def _normalize_series_key(name: str) -> str:
    """Case- and whitespace-insensitive key for grouping series."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


SERIES_PREVIEW_LIMIT = 5


def _format_series_sequence(sequence: "float | int | str | None") -> str:
    """Render a series sequence as a short label ('1', '2.5'); blank when unset."""
    if sequence is None:
        return ""
    try:
        value = float(sequence)
    except (TypeError, ValueError):
        return str(sequence)
    if value.is_integer():
        return str(int(value))
    return ("%.2f" % value).rstrip("0").rstrip(".")


def _series_preview_books(children: list, next_index: "int | None",
                          limit: int = SERIES_PREVIEW_LIMIT) -> "tuple[list, int]":
    """Pick the child books a collapsed series card previews.

    A series longer than the limit scrolls the window to the next unfinished book,
    so the preview shows where the reader actually is rather than volumes they
    finished long ago.
    """
    total = len(children)
    if total > limit and next_index is not None:
        start = max(0, min(next_index - 1, total - limit))
    else:
        start = 0
    window = children[start:start + limit]

    preview = []
    for offset, child in enumerate(window):
        position = start + offset
        progress = child.get("unified_progress") or 0
        preview.append({
            "abs_id": child.get("abs_id"),
            "label": _format_series_sequence(child.get("series_sequence")) or str(position + 1),
            "title": child.get("display_title") or "",
            "progress": progress,
            "is_next": next_index is not None and position == next_index,
        })
    return preview, total - len(window)


def _finalize_series_group(group: dict) -> None:
    """Compute aggregate display fields for a series group in-place."""
    from collections import Counter
    children = group["children"]
    children.sort(key=lambda c: (
        c.get("series_sequence") if c.get("series_sequence") is not None else float("inf"),
        (c.get("display_title") or "").casefold(),
    ))

    total = len(children)
    finished = sum(1 for c in children if (c.get("unified_progress") or 0) >= 100)
    in_progress = sum(1 for c in children if 0 < (c.get("unified_progress") or 0) < 100)
    avg = round(sum((c.get("unified_progress") or 0) for c in children) / total, 1) if total else 0.0
    next_index = next(
        (i for i, c in enumerate(children) if (c.get("unified_progress") or 0) < 100),
        None,
    )
    next_book = children[next_index] if next_index is not None else None
    preview_books, preview_hidden_count = _series_preview_books(children, next_index)

    last_sync_unix = 0.0
    for c in children:
        ts = c.get("last_sync_unix") or 0.0
        if ts > last_sync_unix:
            last_sync_unix = ts

    # A series sorts by its most recent addition, so adding one book to a series
    # started long ago surfaces the whole group rather than burying it.
    added_at_unix = 0.0
    for c in children:
        ts = c.get("added_at_unix") or 0.0
        if ts > added_at_unix:
            added_at_unix = ts

    author_counts = Counter(
        (c.get("display_author") or "").strip() for c in children if c.get("display_author")
    )
    if author_counts:
        group["series_author"] = author_counts.most_common(1)[0][0]

    group.update({
        "child_count": total,
        "finished_count": finished,
        "in_progress_count": in_progress,
        "avg_progress": avg,
        "next_book": next_book,
        "preview_books": preview_books,
        "preview_hidden_count": preview_hidden_count,
        "last_sync_unix": last_sync_unix,
        "added_at_unix": added_at_unix,
        "stack_cover_urls": [c.get("cover_url") for c in children[:3] if c.get("cover_url")],
        "section_bucket": "finished" if finished == total else "not_started",
        "dom_id": "series-" + re.sub(r"[^a-z0-9]+", "-", group["series_key"]).strip("-"),
    })


def _group_dashboard_mappings_by_series(mappings: list) -> list:
    """
    Convert flat mapping list into a mixed list of flat mappings and series group dicts.
    Groups with only one child are demoted back to flat mappings.
    """
    groups = {}
    order = []

    for m in mappings:
        series_name = (m.get("series_name") or "").strip()
        key = _normalize_series_key(series_name)
        if not key:
            entry_id = id(m)
            order.append(("single", entry_id))
            groups[entry_id] = m
            continue
        if key not in groups:
            groups[key] = {
                "is_series_group": True,
                "series_name": series_name,
                "series_key": key,
                "series_author": m.get("display_author") or "",
                "children": [],
            }
            order.append(("series", key))
        groups[key]["children"].append(m)

    result = []
    for kind, key in order:
        entry = groups[key]
        if kind == "single":
            result.append(entry)
        elif len(entry["children"]) == 1:
            result.append(entry["children"][0])
        else:
            _finalize_series_group(entry)
            result.append(entry)
    return result


def _dashboard_filename_key(filename):
    value = (filename or "").strip()
    return value.casefold() if value else ""


def _index_cached_booklore_books(all_booklore_books):
    indexed = {}
    for cached in all_booklore_books or []:
        key = _dashboard_filename_key(getattr(cached, "filename", None))
        if key and key not in indexed:
            indexed[key] = cached
    return indexed


def _get_cached_booklore_book(book, cached_booklore_by_filename=None):
    candidates = []
    for filename in (
        getattr(book, "original_ebook_filename", None),
        getattr(book, "ebook_filename", None),
    ):
        if filename and filename not in candidates:
            candidates.append(filename)

    for filename in candidates:
        if cached_booklore_by_filename is not None:
            cached = cached_booklore_by_filename.get(_dashboard_filename_key(filename))
        else:
            cached = database_service.get_booklore_book(filename)
        if cached:
            return cached
    return None


def _get_cached_ebook_display_metadata(book, cached_booklore_by_filename=None):
    cached = _get_cached_booklore_book(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if not cached:
        return {}
    raw = cached.raw_metadata_dict if hasattr(cached, "raw_metadata_dict") and isinstance(cached.raw_metadata_dict, dict) else {}
    title = _normalize_dashboard_display_value(raw.get("title") or getattr(cached, "title", ""))
    subtitle = _normalize_dashboard_display_value(raw.get("subtitle"))
    author = _coerce_author_display(raw.get("authors")) or _normalize_dashboard_display_value(getattr(cached, "authors", ""))
    if title or subtitle or author:
        return {"title": title, "subtitle": subtitle, "author": author}
    return {}


def _coerce_dashboard_rating(value):
    if value in (None, ""):
        return None
    try:
        rating = float(str(value).replace(",", "").strip())
    except Exception:
        return None
    if rating < 0:
        return None
    return rating


def _coerce_dashboard_count(value):
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except Exception:
        return None


def _get_cached_goodreads_rating(book, cached_booklore_by_filename=None):
    cached = _get_cached_booklore_book(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if not cached:
        return {}

    raw = cached.raw_metadata_dict if hasattr(cached, "raw_metadata_dict") and isinstance(cached.raw_metadata_dict, dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}

    rating = _coerce_dashboard_rating(metadata.get("goodreadsRating") or raw.get("goodreadsRating"))
    review_count = _coerce_dashboard_count(metadata.get("goodreadsReviewCount") or raw.get("goodreadsReviewCount"))
    if rating is None and review_count is None:
        return {}
    return {
        "goodreads_rating": rating,
        "goodreads_review_count": review_count,
    }


def _get_cached_booklore_id(book, cached_booklore_by_filename=None):
    cached = _get_cached_booklore_book(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if not cached:
        return None
    raw = cached.raw_metadata_dict if hasattr(cached, "raw_metadata_dict") and isinstance(cached.raw_metadata_dict, dict) else {}
    for key in ("id", "bookId"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _get_dashboard_display_filename(book):
    for filename in (
        getattr(book, "original_ebook_filename", None),
        getattr(book, "ebook_filename", None),
    ):
        value = (filename or "").strip()
        if value:
            return value
    return ""


def _normalize_dashboard_display_value(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.strip())


def _parse_dashboard_filename_fallback(filename):
    display_filename = (filename or "").strip()
    stem = Path(display_filename).stem.strip() if display_filename else ""
    if not stem:
        return {
            "display_title": "",
            "display_subtitle": "",
            "display_author": "",
            "display_filename": display_filename,
        }

    if " - " in stem:
        title_part, author_part = stem.rsplit(" - ", 1)
        title_part = title_part.strip()
        author_part = re.sub(r"\s*\(\d{4}\)\s*$", "", author_part).strip()
        if title_part and author_part:
            return {
                "display_title": title_part,
                "display_subtitle": "",
                "display_author": author_part,
                "display_filename": display_filename,
            }

    return {
        "display_title": stem,
        "display_subtitle": "",
        "display_author": "",
        "display_filename": display_filename,
    }


def _looks_like_dashboard_filename_title(title):
    normalized_title = _normalize_dashboard_display_value(title)
    if not normalized_title:
        return False
    parsed = _parse_dashboard_filename_fallback(normalized_title)
    return bool(parsed.get("display_author"))


def _should_override_dashboard_base_title(book, base_title, display_filename):
    normalized_title = _normalize_dashboard_display_value(base_title)
    if getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return True
    if not normalized_title:
        return True
    if normalized_title.lower().startswith("storyteller_"):
        return True

    filename_stem = Path(display_filename).stem if display_filename else ""
    if filename_stem and normalized_title.casefold() == _normalize_dashboard_display_value(filename_stem).casefold():
        return True

    return _looks_like_dashboard_filename_title(normalized_title)


def _get_storyteller_display_metadata(storyteller_uuid):
    if not storyteller_uuid:
        return {}
    try:
        st_client = uc().storyteller_client
        if not st_client or not st_client.is_configured() or not hasattr(st_client, "get_book_details"):
            return {}
        details = st_client.get_book_details(storyteller_uuid) or {}
        return {
            "title": (details.get("title") or "").strip(),
            "subtitle": (details.get("subtitle") or "").strip(),
            "author": _coerce_author_display(details.get("authors")),
        }
    except Exception as exc:
        logger.debug("Storyteller metadata lookup failed for '%s': %s", storyteller_uuid, exc)
        return {}


def _get_cached_storyteller_display_metadata(book):
    raw_title = _normalize_dashboard_display_value(getattr(book, "audio_title", None))
    if not raw_title:
        return {}
    display_filename = _get_dashboard_display_filename(book)
    if not _should_override_dashboard_base_title(book, raw_title, display_filename):
        return {}
    return {
        "title": raw_title,
        "subtitle": "",
        "author": "",
    }


def _resolve_dashboard_display_metadata(
    book,
    base_title,
    base_subtitle,
    base_author,
    cached_booklore_by_filename=None,
    storyteller_meta=None,
    bookorbit_author=None,
):
    title = _normalize_dashboard_display_value(base_title)
    subtitle = _normalize_dashboard_display_value(base_subtitle)
    author = _normalize_dashboard_display_value(base_author)
    display_filename = _get_dashboard_display_filename(book)
    should_override_base_title = _should_override_dashboard_base_title(book, title, display_filename)
    original_title = title

    cached_meta = _get_cached_ebook_display_metadata(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if cached_meta:
        cached_title = _normalize_dashboard_display_value(cached_meta.get("title"))
        cached_subtitle = _normalize_dashboard_display_value(cached_meta.get("subtitle"))
        cached_author = _normalize_dashboard_display_value(cached_meta.get("author"))
        if should_override_base_title and title == original_title and cached_title:
            title = cached_title
        if not subtitle and cached_subtitle:
            subtitle = cached_subtitle
        if not author and cached_author:
            author = cached_author

    storyteller_meta = storyteller_meta or {}
    if storyteller_meta:
        storyteller_title = _normalize_dashboard_display_value(storyteller_meta.get("title"))
        storyteller_subtitle = _normalize_dashboard_display_value(storyteller_meta.get("subtitle"))
        storyteller_author = _normalize_dashboard_display_value(storyteller_meta.get("author"))
        if should_override_base_title and title == original_title and storyteller_title:
            title = storyteller_title
        if not subtitle and storyteller_subtitle:
            subtitle = storyteller_subtitle
        if not author and storyteller_author:
            author = storyteller_author

    # Ranked below the Grimmory/Storyteller caches but above the filename guess:
    # it is real library metadata, so it should only ever fill a gap, never
    # displace an author another source already resolved.
    if not author and bookorbit_author:
        author = _normalize_dashboard_display_value(bookorbit_author)

    filename_fallback = _parse_dashboard_filename_fallback(display_filename)
    if should_override_base_title and not title:
        title = filename_fallback["display_title"]
    if should_override_base_title and title == original_title and filename_fallback["display_author"]:
        title = filename_fallback["display_title"]
    if not author and filename_fallback["display_author"]:
        author = filename_fallback["display_author"]

    return {
        "display_title": title or filename_fallback["display_title"] or _normalize_dashboard_display_value(base_title),
        "display_subtitle": subtitle,
        "display_author": author,
        "display_filename": display_filename,
    }


def _storyteller_transcript_source(storyteller_uuid, storyteller_manifest):
    return "storyteller" if storyteller_uuid or storyteller_manifest else None


def _get_dashboard_sync_warning_clients(mapping, integrations):
    client_names = []

    # A book repointed away from ABS keeps its old ABS progress row so the
    # repoint stays undoable, but the sync engine never refreshes that row and
    # the dashboard hides its tile — counting it would flag permanent drift
    # nothing can clear.
    if (
        integrations.get('abs')
        and mapping.get('sync_mode') != 'ebook_only'
        and (mapping.get('audio_source') or 'ABS') == 'ABS'
    ):
        client_names.append('abs')

    if integrations.get('bookloreaudio') and mapping.get('audio_source') == 'BookLore':
        client_names.append('bookloreaudio')

    if integrations.get('bookorbitaudio') and mapping.get('audio_source') == 'BookOrbit':
        client_names.append('bookorbitaudio')

    if integrations.get('kosync'):
        client_names.append('kosync')

    if integrations.get('storyteller') and (
        mapping.get('storyteller_uuid')
        or mapping.get('storyteller_legacy_link')
        or 'storyteller' in mapping.get('states', {})
    ):
        client_names.append('storyteller')

    if integrations.get('booklore') and (
        mapping.get('booklore_id')
        or 'booklore' in mapping.get('states', {})
    ):
        client_names.append('booklore')

    if integrations.get('bookorbit') and (
        mapping.get('ebook_source') == 'BookOrbit'
        or 'bookorbit' in mapping.get('states', {})
    ):
        client_names.append('bookorbit')

    if integrations.get('kavita') and (
        mapping.get('ebook_source') == 'Kavita'
        or 'kavita' in mapping.get('states', {})
    ):
        client_names.append('kavita')

    return client_names


# Clients that report progress on the audio-time axis (elapsed seconds /
# duration) rather than the ebook-text axis (characters / total). Their raw
# percentage is not directly comparable to ebook clients, so it is mapped onto
# the text axis via the alignment map before the drift comparison.
_AUDIO_AXIS_SYNC_CLIENTS = {'abs', 'bookloreaudio', 'bookorbitaudio'}


def _dashboard_text_axis_pct(client_name, state, mapping):
    """Return a client's progress as an ebook text-axis percentage (0-100).

    Ebook clients already report on the text axis. Audio clients report on the
    time axis; convert their timestamp to a text fraction via the book's
    alignment map so the two are comparable. Falls back to the raw percentage
    when no alignment map is available (e.g. audio-only books)."""
    percentage = state.get('percentage')
    if percentage is None:
        return None

    if client_name not in _AUDIO_AXIS_SYNC_CLIENTS:
        return float(percentage)

    alignment_service = getattr(manager, "alignment_service", None) if manager else None
    if not alignment_service:
        return float(percentage)

    timestamp = state.get('timestamp') or 0
    if timestamp <= 0:
        duration = mapping.get('duration') or 0
        if duration > 0:
            timestamp = (float(percentage) / 100.0) * duration
    if timestamp <= 0:
        return float(percentage)

    try:
        text_fraction = alignment_service.get_progress_for_time(mapping.get('abs_id'), float(timestamp))
    except Exception:
        text_fraction = None

    if not isinstance(text_fraction, (int, float)) or isinstance(text_fraction, bool):
        return float(percentage)
    return text_fraction * 100.0


# Drift is computed for every visible book on every dashboard render, and an
# audio client's conversion loads that book's alignment map — a 10-15MB JSON blob
# against a 3-entry cache. The result only moves when a position moves, so it is
# memoized on the exact inputs that produced it (issue #412).
_DASHBOARD_SYNC_WARNING_CACHE = LRUCache(capacity=512)


def _dashboard_sync_warning_candidates(mapping, integrations):
    """The (client, state) pairs eligible for the drift comparison.

    Split out from the computation so callers can count the candidates — and bail
    — before paying for any audio-to-text axis conversion."""
    candidates = []
    states = mapping.get('states', {})

    for client_name in _get_dashboard_sync_warning_clients(mapping, integrations):
        state = states.get(client_name)
        if not state:
            continue
        raw = state.get('percentage')
        if raw is None or raw <= 0:
            continue
        candidates.append((client_name, state))

    return candidates


def _dashboard_sync_warning_cache_key(mapping, candidates):
    """Fingerprint the inputs the drift number is derived from.

    Anything that can change the answer belongs here: the book, the set of
    candidate clients, each one's reported position, and the duration used as the
    timestamp fallback. Identical fingerprint, identical result."""
    return (
        mapping.get('abs_id'),
        mapping.get('duration') or 0,
        tuple(
            (name, state.get('percentage'), state.get('timestamp'))
            for name, state in candidates
        ),
    )


def _compute_dashboard_sync_warning_pct(mapping, integrations):
    candidates = _dashboard_sync_warning_candidates(mapping, integrations)

    # One reporting client cannot drift from anything. Returning here keeps a
    # single-integration install off the alignment path entirely.
    if len(candidates) < 2:
        return 0.0

    cache_key = _dashboard_sync_warning_cache_key(mapping, candidates)
    cached = _DASHBOARD_SYNC_WARNING_CACHE.get(cache_key)
    if cached is not None:
        return cached

    progress_values = []
    for client_name, state in candidates:
        value = _dashboard_text_axis_pct(client_name, state, mapping)
        if value is None:
            continue
        progress_values.append(value)

    if len(progress_values) < 2:
        return 0.0

    warning_pct = round(max(progress_values) - min(progress_values), 1)
    _DASHBOARD_SYNC_WARNING_CACHE.put(cache_key, warning_pct)
    return warning_pct


def _shelf_watch_clients_for(meta: dict):
    """Resolve (library_client, watch_shelf, kobo_shelf) for a shelf-watch
    suggestion based on its origin source (Grimmory vs BookOrbit)."""
    source = (meta or {}).get('source_name') or 'BookLore'
    clients = uc()
    if source == 'BookOrbit':
        return (
            clients.bookorbit_client,
            os.environ.get('BOOKORBIT_SHELF_WATCH_NAME', 'Up Next'),
            user_setting('BOOKORBIT_SHELF_NAME', 'Kobo'),
        )
    return (
        clients.booklore_client,
        os.environ.get('BOOKLORE_SHELF_WATCH_NAME', 'Up Next'),
        user_setting('BOOKLORE_SHELF_NAME', 'Kobo'),
    )


def _queue_item_shelf_watch_metadata(item: dict) -> "dict | None":
    """Detach shelf-watch origin metadata before mapping helpers dismiss it."""
    keys = []
    for raw_key in (item.get('bridge_key'), item.get('abs_id')):
        key = str(raw_key or '').strip()
        if key and key not in keys:
            keys.append(key)

    for key in keys:
        try:
            pending = database_service.get_pending_suggestion(key)
        except Exception as exc:
            logger.warning("Shelf-watch approval lookup failed for '%s': %s", key, exc, exc_info=True)
            continue
        if pending and getattr(pending, 'origin', None) == 'shelf_watch':
            metadata = pending.origin_metadata or {}
            return dict(metadata) if isinstance(metadata, dict) else None
    return None


def _complete_shelf_watch_approval(meta: dict, *, remove_only: bool = False) -> bool:
    """Finish the recorded shelf-watch move without failing a saved mapping."""
    if not meta:
        return False
    filename = meta.get('grimmory_filename')
    if not filename:
        return False
    try:
        library_client, watch_shelf, kobo_shelf = _shelf_watch_clients_for(meta)
        if not library_client or not library_client.is_configured():
            return False
        if watch_shelf == kobo_shelf:
            return True
        if remove_only:
            success = library_client.remove_from_shelf(filename, watch_shelf)
            action = f"remove from '{watch_shelf}'"
        else:
            success = library_client.move_between_shelves(filename, watch_shelf, kobo_shelf)
            action = f"move from '{watch_shelf}' to '{kobo_shelf}'"
        if success is False:
            logger.warning(
                "Shelf-watch approval failed to %s for '%s'",
                action,
                sanitize_log_data(filename),
            )
            return False
        return True
    except Exception as exc:
        logger.warning(
            "Shelf-watch approval failed for '%s': %s",
            sanitize_log_data(filename),
            exc,
            exc_info=True,
        )
        return False


def _format_dashboard_last_sync(latest_update_time):
    if latest_update_time <= 0:
        return "Never"
    diff = time.time() - latest_update_time
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    return f"{int(diff // 3600)}h ago"


def _build_dashboard_integrations():
    integrations = {}
    sync_clients = uc().sync_clients
    client_items = sync_clients.items() if hasattr(sync_clients, "items") else sync_clients
    try:
        iterator = list(client_items)
    except TypeError:
        return integrations
    for client_name, client in iterator:
        integrations[client_name.lower()] = bool(client.is_configured())
    return integrations


def _group_dashboard_states_by_book(all_states):
    states_by_book = {}
    for state in all_states or []:
        states_by_book.setdefault(state.abs_id, []).append(state)
    return states_by_book


def _dashboard_leader_service(leader_client: str | None) -> str | None:
    """Map a raw ReadingSession.leader_client to the dashboard's service key.

    The returned key matches the lowercase client name used in ``mapping["states"]``
    and the per-service ``.service-item`` blocks in ``index.html`` (``'abs'``,
    ``'kosync'``, ``'storyteller'``, ``'booklore'``, ``'bookloreaudio'``,
    ``'bookorbit'``, ``'bookorbitaudio'``, ``'kavita'``, ``'cwa'``), so the 'In Progress' card can
    subtly dot whichever service last moved the position.

    This is display-only: internal client keys are never renamed. KoSync device
    variants (``KoSync:<device>``, ``BridgeSync_Plugin``) collapse to ``'kosync'``;
    ``ABSEbook`` shares the single ABS row; audio leaders keep their own row (the
    template renders distinct GR/BO Audio rows); unknown / write-only tracker
    leaders return None (no dot).
    """
    if not leader_client:
        return None
    key = str(leader_client).strip().lower()
    if not key:
        return None
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "kosync"
    if key in ("abs", "absebook"):
        return "abs"
    if key == "storyteller":
        return "storyteller"
    if key == "booklore":
        return "booklore"
    if key == "bookloreaudio":
        return "bookloreaudio"
    if key == "bookorbit":
        return "bookorbit"
    if key == "bookorbitaudio":
        return "bookorbitaudio"
    if key == "kavita":
        return "kavita"
    if key == "cwa":
        return "cwa"
    return None


def _browser_cover_url(
    raw_cover_url: str | None,
    audio_source: str | None = None,
    audio_source_id: str | None = None,
    abs_id: str | None = None,
    ebook_source: str | None = None,
    ebook_source_id: str | None = None,
) -> str:
    """Convert any cover URL into a safe, same-origin BookBridge URL.

    This is the single choke point that keeps source hostnames and API tokens
    out of the browser (issue #353). It also neutralizes legacy
    ``audio_cover_url`` values already saved in the database, so no migration
    is needed.

    Priority:
    1. If ``raw_cover_url`` is a non-empty same-origin relative path (starts
       with a single ``/`` but not ``//`` or ``/\\``), return it unchanged.
       Backslashes are rejected because browsers normalize them to forward
       slashes when resolving URLs, so ``/\\evil.example`` would resolve as a
       protocol-relative cross-origin URL.
    2. Otherwise derive a same-origin proxy route from the source:
       - BookLore with ``audio_source_id`` -> ``/api/booklore/audiobook-cover/<id>``
       - BookOrbit with ``audio_source_id`` -> ``/api/bookorbit/audiobook-cover/<id>``
       - ABS or unset source with ``abs_id`` (preferred) or ``audio_source_id`` ->
         ``/api/cover-proxy/<id>`` (only for non-library audio sources)
    3. Failing that, derive from the ebook library that hosts the book, so
       ebook-only mappings still get art: BookOrbit/BookLore with
       ``ebook_source_id`` reuse that provider's cover proxy (one endpoint
       serves a book's cover whether the book is audio or text). CWA, Kavita
       and local files expose no id we can proxy, so they stay coverless.
    4. If nothing can be derived, return an empty string.

    An ebook-only mapping has no Audiobookshelf item, so its synthetic
    ``ebook-<hash>`` key must not be turned into a cover-proxy URL that can only
    404. Legacy rows whose ``audio_source`` is unset but whose id is a real ABS
    item are still served, so the test is on the id, not the source.
    """
    raw = (raw_cover_url or "").strip()
    if raw.startswith("/") and not raw.startswith("//") and not raw.startswith("/\\"):
        return raw

    source = (audio_source or "").strip()
    src_id = (audio_source_id or "").strip()
    aid = (abs_id or "").strip()

    if source == "BookLore" and src_id:
        return f"/api/booklore/audiobook-cover/{src_id}"
    if source == "BookOrbit" and src_id:
        return f"/api/bookorbit/audiobook-cover/{src_id}"
    if source not in _LIBRARY_AUDIO_SOURCES:
        proxy_id = aid or src_id
        if proxy_id and not _is_synthetic_bridge_key(proxy_id):
            return f"/api/cover-proxy/{proxy_id}"

    ebook_src = (ebook_source or "").strip()
    ebook_id = (ebook_source_id or "").strip()
    if ebook_id:
        if is_grimmory_source(ebook_src):
            return f"/api/booklore/audiobook-cover/{ebook_id}"
        if ebook_src == "BookOrbit":
            return f"/api/bookorbit/audiobook-cover/{ebook_id}"
    return ""


def _is_synthetic_bridge_key(candidate: str) -> bool:
    """True when an id is a bridge-minted key rather than an ABS item id.

    Ebook-only mappings use ``ebook-<kosync_doc_id[:16]>`` (and ``ebook:<key>``
    in the match queue); library audiobooks use ``booklore:``/``bookorbit:``.
    None of these can be fetched from Audiobookshelf.
    """
    key = (candidate or "").strip().lower()
    if key.startswith(("ebook-", "ebook:")):
        return True
    return any(key.startswith(f"{prefix}:") for prefix in _AUDIO_BRIDGE_PREFIXES)


def _sanitize_cover_urls(entries: list) -> list:
    """Return copies of suggestion/queue dicts with browser-safe cover URLs.

    Suggestion and match-queue entries can be restored from a scan cache
    persisted by an older build, so they may still carry tokenized source URLs
    (issue #353). The originals are left untouched because they are shared with
    the session state and the on-disk cache.
    """
    sanitized = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            sanitized.append(entry)
            continue
        keys = [key for key in ("audio_cover_url", "cover_url") if key in entry]
        if not keys:
            sanitized.append(entry)
            continue
        safe_cover = _browser_cover_url(
            entry.get("audio_cover_url") or entry.get("cover_url"),
            audio_source=entry.get("audio_source"),
            audio_source_id=entry.get("audio_source_id"),
            abs_id=entry.get("bridge_key") or entry.get("abs_id"),
        )
        copied = dict(entry)
        for key in keys:
            copied[key] = safe_cover
        sanitized.append(copied)
    return sanitized


def _public_link_base(web_url_key: str, server_fallback: str) -> str:
    """Browser-facing base URL for a service, falling back to its server URL.

    A service's server URL is what BookBridge calls, and on a Docker network that
    is routinely a name the browser cannot resolve (`audiobookshelf:80`). The
    optional `*_WEB_URL` setting is the address to send a browser to instead.
    Every link rendered for a user goes through here so a public URL can't be
    honoured on one link and silently ignored on another.
    """
    return (os.environ.get(web_url_key, '') or '').strip().rstrip('/') or (server_fallback or '').rstrip('/')


def _prefetch_bookfusion_links(books: list, integrations: dict | None) -> dict:
    """Resolve every book's BookFusion link in one query instead of one per book.

    Returns ``{abs_id: link_dict}``, empty when BookFusion is unconfigured or no
    user resolves. The dashboard template gates the BookFusion tile on the same
    ``integrations['bookfusion']`` flag, so skipping the query when that flag is
    falsey changes nothing the user sees.
    """
    if not integrations or not integrations.get('bookfusion'):
        return {}
    user_id = _active_bookfusion_link_user_id()
    if user_id is None:
        return {}
    abs_ids = [
        abs_id for abs_id in (getattr(book, "abs_id", None) for book in (books or [])) if abs_id
    ]
    if not abs_ids:
        return {}
    try:
        return database_service.get_user_bookfusion_links_for_books(user_id, abs_ids) or {}
    except Exception as exc:
        logger.debug("BookFusion dashboard link prefetch failed: %s", exc, exc_info=True)
        return {}


def _bookorbit_source_ids(books: list) -> set:
    """BookOrbit book ids referenced by these mappings, either side."""
    ids = set()
    for book in books or []:
        for attr, source in (("ebook_source_id", "ebook_source"),
                             ("audio_source_id", "audio_source")):
            if getattr(book, source, None) == "BookOrbit":
                value = getattr(book, attr, None)
                if value:
                    ids.add(str(value))
    return ids


def _prefetch_bookorbit_authors(books: list, integrations: dict | None) -> dict:
    """Resolve BookOrbit's own author metadata in one bulk read.

    Returns ``{bookorbit_book_id: author}``, empty when BookOrbit is unconfigured
    or nothing in the library came from it.

    BookOrbit is the only author source for a BookOrbit-sourced ebook on an
    install without Grimmory: ``base_author`` is always empty here,
    ``_get_cached_ebook_display_metadata`` reads the Grimmory cache alone, and the
    filename fallback needs a literal ``Title - Author`` stem that BookOrbit's
    filenames ("03. Other Worlds Than These (2026).epub") do not use. The client
    keeps its own TTL'd light-info cache behind a non-blocking refresh lock, so
    this is a dict read once warm and a couple of paginated calls when cold.
    """
    if not integrations or not integrations.get('bookorbit'):
        return {}
    wanted = _bookorbit_source_ids(books)
    if not wanted:
        return {}
    try:
        client = uc().bookorbit_client
        if not client or not client.is_configured():
            return {}
        authors = {}
        for info in client.get_all_books() or []:
            book_id = info.get("id")
            if book_id is None:
                continue
            key = str(book_id)
            if key not in wanted:
                continue
            author = _normalize_dashboard_display_value(info.get("authors"))
            if author:
                authors[key] = author
        return authors
    except Exception as exc:
        logger.debug("BookOrbit dashboard author prefetch failed: %s", exc, exc_info=True)
        return {}


def _bookorbit_author_for_book(book, bookorbit_authors: dict | None) -> str:
    """The prefetched author for whichever side of this book BookOrbit supplies."""
    if not bookorbit_authors:
        return ""
    for attr, source in (("ebook_source_id", "ebook_source"),
                         ("audio_source_id", "audio_source")):
        if getattr(book, source, None) == "BookOrbit":
            value = getattr(book, attr, None)
            if value:
                found = bookorbit_authors.get(str(value))
                if found:
                    return found
    return ""


def _build_dashboard_mapping(
    book,
    states_by_book,
    integrations,
    hardcover_by_book,
    storygraph_by_book,
    reading_stats_by_book,
    cached_booklore_by_filename,
    claim_times_by_book=None,
    bookfusion_by_book=None,
    bookorbit_authors=None,
):
    states = states_by_book.get(book.abs_id, [])
    state_by_client = {state.client_name: state for state in states}

    display_meta = _resolve_dashboard_display_metadata(
        book,
        getattr(book, "audio_title", None) or book.abs_title,
        "",
        "",
        cached_booklore_by_filename=cached_booklore_by_filename,
        storyteller_meta=_get_cached_storyteller_display_metadata(book),
        bookorbit_author=_bookorbit_author_for_book(book, bookorbit_authors),
    )
    display_title = display_meta["display_title"]
    display_subtitle = display_meta["display_subtitle"]
    display_author = display_meta["display_author"]

    mapping = {
        "abs_id": book.abs_id,
        "abs_title": display_title,
        "abs_subtitle": display_subtitle,
        "abs_author": display_author,
        "display_title": display_title,
        "display_subtitle": display_subtitle,
        "display_author": display_author,
        "display_filename": display_meta["display_filename"],
        "audio_source": getattr(book, "audio_source", None) or ("ABS" if getattr(book, "sync_mode", "audiobook") != "ebook_only" else None),
        "audio_source_id": getattr(book, "audio_source_id", None) or book.abs_id,
        "audio_title": getattr(book, "audio_title", None) or display_title,
        "audio_duration": getattr(book, "audio_duration", None) or book.duration or 0,
        "audio_cover_url": getattr(book, "audio_cover_url", None),
        "ebook_filename": book.ebook_filename,
        "original_ebook_filename": getattr(book, "original_ebook_filename", None),
        "has_local_epub": bool(book.original_ebook_filename or book.ebook_filename),
        "ebook_source": getattr(book, "ebook_source", None),
        "ebook_source_id": getattr(book, "ebook_source_id", None),
        "kosync_doc_id": book.kosync_doc_id,
        "transcript_file": book.transcript_file,
        "status": book.status,
        "sync_mode": getattr(book, "sync_mode", "audiobook"),
        "unified_progress": 0,
        "duration": book.duration or 0,
        "storyteller_uuid": book.storyteller_uuid,
        "added_at_unix": (claim_times_by_book or {}).get(book.abs_id, 0.0),
        "states": {},
    }

    if book.status in ("processing", "forging"):
        job = database_service.get_latest_job(book.abs_id)
        if job:
            mapping["job_progress"] = round((job.progress or 0.0) * 100, 1)
            mapping["job_last_error"] = job.last_error
        else:
            mapping["job_progress"] = 0.0

    latest_update_time = 0
    max_progress = 0
    for client_name, state in state_by_client.items():
        if state.last_updated and state.last_updated > latest_update_time:
            latest_update_time = state.last_updated

        pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0
        mapping["states"][client_name] = {
            "timestamp": state.timestamp or 0,
            "percentage": pct_val,
            "last_updated": state.last_updated,
            "xpath": getattr(state, "xpath", None),
        }
        if getattr(state, "cfi", None) is not None:
            mapping["states"][client_name]["cfi"] = getattr(state, "cfi", None)

        if state.percentage is not None:
            max_progress = max(max_progress, pct_val)

        if client_name == "kosync":
            mapping["kosync_pct"] = pct_val
            mapping["kosync_xpath"] = getattr(state, "xpath", None)
        elif client_name == "abs":
            mapping["abs_pct"] = pct_val
            mapping["abs_ts"] = state.timestamp
        elif client_name == "storyteller":
            mapping["storyteller_pct"] = pct_val
            mapping["storyteller_xpath"] = getattr(state, "xpath", None)
        elif client_name == "booklore":
            mapping["booklore_pct"] = pct_val
            mapping["booklore_xpath"] = getattr(state, "xpath", None)

    hardcover_details = hardcover_by_book.get(book.abs_id)
    if hardcover_details:
        mapping.update({
            "hardcover_book_id": hardcover_details.hardcover_book_id,
            "hardcover_slug": hardcover_details.hardcover_slug,
            "hardcover_edition_id": hardcover_details.hardcover_edition_id,
            "hardcover_pages": hardcover_details.hardcover_pages,
            "isbn": hardcover_details.isbn,
            "asin": hardcover_details.asin,
            "matched_by": hardcover_details.matched_by,
            "hardcover_linked": True,
            "hardcover_title": book.abs_title,
        })
    else:
        mapping.update({
            "hardcover_book_id": None,
            "hardcover_slug": None,
            "hardcover_edition_id": None,
            "hardcover_pages": None,
            "isbn": None,
            "asin": None,
            "matched_by": None,
            "hardcover_linked": False,
            "hardcover_title": None,
        })

    storygraph_details = storygraph_by_book.get(book.abs_id)
    if storygraph_details:
        mapping.update({
            "storygraph_book_id": storygraph_details.storygraph_book_id,
            "storygraph_linked": True,
            "storygraph_url": storygraph_details.storygraph_url,
            "storygraph_title": book.abs_title,
            "storygraph_matched_by": storygraph_details.matched_by,
            "storygraph_rating": _coerce_dashboard_rating(getattr(storygraph_details, "storygraph_rating", None)),
            "storygraph_review_count": _coerce_dashboard_count(getattr(storygraph_details, "storygraph_review_count", None)),
        })
    else:
        mapping.update({
            "storygraph_book_id": None,
            "storygraph_linked": False,
            "storygraph_url": None,
            "storygraph_title": None,
            "storygraph_matched_by": None,
            "storygraph_rating": None,
            "storygraph_review_count": None,
        })

    bookfusion_link = (bookfusion_by_book or {}).get(book.abs_id)
    if not isinstance(bookfusion_link, dict):
        bookfusion_link = None
    mapping.update({
        "bookfusion_id": (bookfusion_link or {}).get("bookfusion_id"),
        "bookfusion_title": (bookfusion_link or {}).get("title"),
        "bookfusion_linked": bool(bookfusion_link),
    })

    mapping["storyteller_legacy_link"] = "storyteller" in state_by_client and not book.storyteller_uuid

    if mapping.get("sync_mode") == "ebook_only":
        mapping["abs_url"] = None
        mapping["audio_url"] = None
    elif mapping["audio_source"] == "BookLore":
        mapping["abs_url"] = None
        _bl_audio_base = _public_link_base('BOOKLORE_WEB_URL', manager.booklore_client.base_url)
        mapping["audio_url"] = f"{_bl_audio_base}/book/{mapping['audio_source_id']}?tab=view"
    elif mapping["audio_source"] == "BookOrbit":
        mapping["abs_url"] = None
        _bo_audio_base = _public_link_base('BOOKORBIT_WEB_URL', os.environ.get("BOOKORBIT_SERVER") or "")
        mapping["audio_url"] = f"{_bo_audio_base}/book/{mapping['audio_source_id']}" if _bo_audio_base else None
    else:
        abs_display_base = _public_link_base('ABS_WEB_URL', manager.abs_client.base_url)
        mapping["abs_url"] = f"{abs_display_base}/item/{book.abs_id}"
        mapping["audio_url"] = mapping["abs_url"]

    mapping["booklore_id"] = _get_cached_booklore_id(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if manager.booklore_client.is_configured() and mapping["booklore_id"]:
        booklore_display_base = _public_link_base('BOOKLORE_WEB_URL', manager.booklore_client.base_url)
        mapping["booklore_url"] = f"{booklore_display_base}/book/{mapping['booklore_id']}?tab=view"
    else:
        mapping["booklore_url"] = None

    # BookOrbit deep links — frontend book route is /book/:bookId.
    _bo_base = _public_link_base('BOOKORBIT_WEB_URL', os.environ.get("BOOKORBIT_SERVER") or "")
    if _bo_base and mapping.get("ebook_source") == "BookOrbit" and mapping.get("ebook_source_id"):
        mapping["bookorbit_url"] = f"{_bo_base}/book/{mapping['ebook_source_id']}"
    else:
        mapping["bookorbit_url"] = None
    if _bo_base and mapping.get("audio_source") == "BookOrbit" and mapping.get("audio_source_id"):
        mapping["bookorbit_audio_url"] = f"{_bo_base}/book/{mapping['audio_source_id']}"
    else:
        mapping["bookorbit_audio_url"] = None

    # Kavita stores BookBridge's source id at chapter level while its browser
    # route is series-based. Link to the configured web root rather than guess.
    _kavita_base = _public_link_base('KAVITA_WEB_URL', os.environ.get("KAVITA_SERVER") or "")
    mapping["kavita_url"] = (
        _kavita_base
        if _kavita_base and mapping.get("ebook_source") == "Kavita"
        else None
    )

    mapping.update({
        "goodreads_rating": None,
        "goodreads_review_count": None,
    })
    mapping.update(_get_cached_goodreads_rating(book, cached_booklore_by_filename=cached_booklore_by_filename))

    if mapping.get("hardcover_slug"):
        mapping["hardcover_url"] = f"https://hardcover.app/books/{mapping['hardcover_slug']}"
    elif mapping.get("hardcover_book_id"):
        mapping["hardcover_url"] = f"https://hardcover.app/books/{mapping['hardcover_book_id']}"
    else:
        mapping["hardcover_url"] = None

    if not mapping.get("storygraph_url") and mapping.get("storygraph_book_id"):
        mapping["storygraph_url"] = f"https://app.thestorygraph.com/books/{mapping['storygraph_book_id']}"

    mapping["sync_warning_pct"] = _compute_dashboard_sync_warning_pct(mapping, integrations)
    mapping["is_out_of_sync"] = mapping["sync_warning_pct"] > 5.0
    mapping["unified_progress"] = min(max_progress, 100.0)
    mapping["last_sync"] = _format_dashboard_last_sync(latest_update_time)
    mapping["last_sync_unix"] = latest_update_time
    mapping["series_name"] = getattr(book, "series_name", None) or None
    mapping["series_sequence"] = getattr(book, "series_sequence", None)

    safe_cover = _browser_cover_url(
        mapping.get("audio_cover_url"),
        audio_source=mapping.get("audio_source"),
        audio_source_id=mapping.get("audio_source_id"),
        abs_id=book.abs_id,
    )
    # An ebook-only mapping has no audiobook to take a cover from, so fall back
    # to the library hosting the ebook. `audio_cover_url` stays audio-only.
    display_cover = safe_cover or _browser_cover_url(
        None,
        ebook_source=mapping.get("ebook_source"),
        ebook_source_id=mapping.get("ebook_source_id"),
    )
    if display_cover:
        mapping["cover_url"] = display_cover
    mapping["audio_cover_url"] = safe_cover

    reading_stats = reading_stats_by_book.get(book.abs_id)
    if reading_stats:
        mapping["reading_stats"] = reading_stats
        mapping["last_leader"] = reading_stats.get("last_leader")
        mapping["last_leader_service"] = _dashboard_leader_service(reading_stats.get("last_leader"))

    return mapping


def _build_dashboard_mappings(
    books,
    all_states,
    integrations,
    all_hardcover=None,
    all_storygraph=None,
    reading_stats_by_book=None,
    cached_booklore_by_filename=None,
    claim_times_by_book=None,
    bookfusion_by_book=None,
    bookorbit_authors=None,
):
    hardcover_by_book = {h.abs_id: h for h in (all_hardcover or [])}
    storygraph_by_book = {s.abs_id: s for s in (all_storygraph or [])}
    states_by_book = _group_dashboard_states_by_book(all_states)
    reading_stats_by_book = reading_stats_by_book or {}
    cached_booklore_by_filename = cached_booklore_by_filename or {}
    claim_times_by_book = claim_times_by_book or {}
    if bookfusion_by_book is None:
        bookfusion_by_book = _prefetch_bookfusion_links(books, integrations)
    if bookorbit_authors is None:
        bookorbit_authors = _prefetch_bookorbit_authors(books, integrations)

    ctc_aligned_book_ids = database_service.get_ctc_aligned_book_ids()
    mappings = []
    total_duration = 0
    total_listened = 0

    for book in books:
        mapping = _build_dashboard_mapping(
            book,
            states_by_book,
            integrations,
            hardcover_by_book,
            storygraph_by_book,
            reading_stats_by_book,
            cached_booklore_by_filename,
            claim_times_by_book,
            bookfusion_by_book=bookfusion_by_book,
            bookorbit_authors=bookorbit_authors,
        )
        mapping["ctc_aligned"] = book.abs_id in ctc_aligned_book_ids
        mappings.append(mapping)

        duration = mapping.get("duration", 0)
        progress_pct = mapping.get("unified_progress", 0)
        if duration > 0:
            total_duration += duration
            total_listened += (progress_pct / 100.0) * duration

    if total_duration > 0:
        overall_progress = round((total_listened / total_duration) * 100, 1)
    elif mappings:
        overall_progress = round(sum(m["unified_progress"] for m in mappings) / len(mappings), 1)
    else:
        overall_progress = 0

    return mappings, overall_progress


def _dashboard_visible_books_for_user(books, user):
    """Show each user only the books they have matched/claimed.

    The catalog row (and its alignment/transcript) is shared, but visibility is
    per-user via `user_books` links — a book can be claimed by several users and
    shows on each of their dashboards. Admins are scoped to their own claimed
    books too (no operator-wide view) per product intent.
    """
    if not user:
        return list(books or [])
    uid = getattr(user, "id", None)
    linked = database_service.get_linked_abs_ids(uid)
    return [book for book in (books or []) if getattr(book, "abs_id", None) in linked]


def _claim_book_for_current_user(abs_id):
    """Link the logged-in user to a book they matched so it shows on their
    dashboard / koplugin manifest. A book can be claimed by multiple users
    (shared catalog). No-op for unauthenticated/global contexts."""
    if not abs_id:
        return
    user = current_user()
    if user is None:
        return
    _claim_book_for_user_id(user.id, abs_id)


def _claim_book_for_user_id(user_id, abs_id):
    """Claim a book for an explicit user id. Used by background workers (batch
    match) where there's no Flask request context for `current_user()`; the id is
    the one bound onto the worker thread via `_spawn_user_background`. No-op when
    there's no user (single-user / login-disabled). Multiple users can claim the
    same shared-catalog book."""
    if not abs_id or user_id is None:
        return
    try:
        # Opt-in household mode: everyone sees every matched book. Only visibility
        # fans out — progress, KoSync docs and stats stay per-user as always.
        if env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
            created = database_service.link_book_to_all_active_users(abs_id)
            if created:
                logger.info(
                    "🔗 Shared book '%s' with %d additional user(s) (share-all-books enabled)",
                    sanitize_log_data(abs_id), created,
                )
            return
        database_service.link_user_book(user_id, abs_id)
    except Exception as e:
        logger.debug("Could not link book '%s' to user %s: %s", abs_id, user_id, e)


def _active_bookfusion_link_user_id() -> int | None:
    """Resolve the user id for user-scoped BookFusion book links."""
    uid = get_current_user_id()
    if uid is not None:
        return uid
    try:
        user = current_user()
    except RuntimeError:
        user = None
    if user is not None:
        return user.id
    try:
        if current_app.config.get('LOGIN_DISABLED'):
            return database_service._default_user_id()
    except RuntimeError:
        pass
    return None


def _persist_bookfusion_link_for_user_id(
    user_id: int,
    abs_id: str,
    source: str,
    source_id: str,
    title: str = None,
    author: str = None,
) -> None:
    """Persist a BookFusion source selection as a per-user remote-book link."""
    if _normalize_text_source_type(source) != "BookFusion" or not user_id or not abs_id or not source_id:
        return
    try:
        database_service.set_user_bookfusion_link(
            user_id,
            abs_id,
            source_id,
            title=title,
            author=author,
        )
    except Exception as exc:
        logger.warning(
            "Could not save BookFusion link for user %s book '%s': %s",
            user_id,
            sanitize_log_data(abs_id),
            exc,
            exc_info=True,
        )


def _persist_bookfusion_link_for_current_user(
    abs_id: str,
    source: str,
    source_id: str,
    title: str = None,
    author: str = None,
) -> None:
    """Persist a BookFusion source selection for the active request/background user."""
    _persist_bookfusion_link_for_user_id(
        _active_bookfusion_link_user_id(),
        abs_id,
        source,
        source_id,
        title=title,
        author=author,
    )


def audiobook_matches_search(ab, search_term):
    """Check if audiobook matches search term (searches title AND author)."""
    import re

    # Normalize: remove punctuation
    def normalize(s):
        return re.sub(r'[^\w\s]', '', s.lower())

    title = normalize(manager.get_abs_title(ab))
    author = normalize(get_abs_author(ab))
    search_norm = normalize(search_term)

    # 1. Standard Search: Search term is in Title or Author (e.g. "Harry" in "Harry Potter")
    if search_norm in title or search_norm in author:
        return True

    # 2. Reverse Search: Title/Author is in Search term (e.g. "Dune" in "Dune Messiah")
    # Enforce a minimum length to prevent short or empty matches (for example, "The" or "It").
    MIN_LEN = 4
    
    if len(title) >= MIN_LEN and title in search_norm: return True
    if len(author) >= MIN_LEN and author in search_norm: return True

    return False

# ---------------- ROUTES ----------------
def index():
    """Dashboard - loads books and progress from database service"""
    user = current_user()
    user_id = user.id if user else None
    books = database_service.get_all_books(user_id=user_id)
    all_states = database_service.get_all_states(
        user_id=user_id
    )
    books = _dashboard_visible_books_for_user(books, user)
    all_hardcover = database_service.get_all_hardcover_details()
    all_storygraph = database_service.get_all_storygraph_details()
    all_reading_stats = database_service.get_all_reading_stats(user_id=user_id)
    cached_booklore_by_filename = _index_cached_booklore_books(database_service.get_all_booklore_books())
    claim_times_by_book = database_service.get_book_claim_times(user_id=user_id)
    integrations = _build_dashboard_integrations()
    mappings, overall_progress = _build_dashboard_mappings(
        books,
        all_states,
        integrations,
        all_hardcover=all_hardcover,
        all_storygraph=all_storygraph,
        reading_stats_by_book=all_reading_stats,
        cached_booklore_by_filename=cached_booklore_by_filename,
        claim_times_by_book=claim_times_by_book,
    )

    suggestions = []
    if current_app.config.get('LOGIN_DISABLED') or (user and getattr(user, "is_admin", False)):
        suggestions = [s for s in database_service.get_all_pending_suggestions() if len(s.matches) > 0]
    grouped_mappings = _group_dashboard_mappings_by_series(mappings)

    latest_version, update_available = get_update_status()

    show_diagnostics_modal = (
        not env_truthy('DIAGNOSTICS_PROMPTED')
        and bool(os.environ.get('DIAGNOSTICS_ENDPOINT_URL', '').strip())
        and (current_app.config.get('LOGIN_DISABLED') or (user and getattr(user, 'is_admin', False)))
    )

    return render_template(
        'index.html',
        mappings=mappings,
        grouped_mappings=grouped_mappings,
        integrations=integrations,
        progress=overall_progress,
        suggestions=suggestions,
        app_version=APP_VERSION,
        update_available=update_available,
        latest_version=latest_version,
        show_diagnostics_modal=show_diagnostics_modal
    )


def shelfmark():
    """Shelfmark handoff - redirects to the configured SHELFMARK_URL."""
    url = os.environ.get("SHELFMARK_URL")
    if not url:
        return redirect(url_for('index'))
    
    # Case-insensitive sanitization for the external destination.
    if not url.lower().startswith(('http://', 'https://')):
        url = f"http://{url}"
        
    return redirect(url)


def forge():
    """Legacy Forge page entry point; the unified Add Book flow owns this UI."""
    return redirect(url_for('add_book'), code=302)


def forge_search_audio():
    """API: Search ABS and Grimmory audiobooks for Forge (returns JSON)."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    try:
        query_lower = query.lower()
        results = []
        found_ids = set()
        clients = uc()  # per-user client bundle (their library/sources), else global

        if clients.booklore_client.is_configured():
            try:
                for book in clients.booklore_client.search_audiobooks(query, include_info=True) or []:
                    book_id = str(book.get("id") or "").strip()
                    if not book_id:
                        continue
                    bridge_key = _build_bridge_key("BookLore", book_id)
                    if bridge_key in found_ids:
                        continue
                    found_ids.add(bridge_key)
                    info = book.get("audiobookInfo") or {}
                    tracks = info.get("tracks") if isinstance(info.get("tracks"), list) else []
                    num_files = len(tracks) or 1
                    total_size_bytes = 0
                    for track in tracks:
                        try:
                            total_size_bytes += int(
                                track.get("sizeBytes")
                                or track.get("size")
                                or track.get("metadata", {}).get("size")
                                or 0
                            )
                        except Exception:
                            continue
                    results.append({
                        "id": bridge_key,
                        "audio_source": "BookLore",
                        "audio_source_id": book_id,
                        "title": book.get("title") or book.get("fileName") or f"Grimmory {book_id}",
                        "author": _coerce_author_display(book.get("authors")),
                        "file_size_mb": round(total_size_bytes / (1024 * 1024), 2) if total_size_bytes else 0,
                        "num_files": num_files,
                        "cover_url": f"/api/booklore/audiobook-cover/{book_id}",
                    })
            except Exception as e:
                logger.warning(f"⚠️ Forge audio Grimmory search failed: {e}", exc_info=True)

        _bo_client = getattr(clients, "bookorbit_client", None)
        if _bo_client and _bo_client.is_configured():
            try:
                for book in _bo_client.search_audiobooks(query) or []:
                    book_id = str(book.get("id") or "").strip()
                    if not book_id:
                        continue
                    bridge_key = _build_bridge_key("BookOrbit", book_id)
                    if bridge_key in found_ids:
                        continue
                    found_ids.add(bridge_key)
                    results.append({
                        "id": bridge_key,
                        "audio_source": "BookOrbit",
                        "audio_source_id": book_id,
                        "title": book.get("title") or f"BookOrbit {book_id}",
                        "author": _coerce_author_display(book.get("authors")),
                        "file_size_mb": round((book.get("total_size_bytes") or 0) / (1024 * 1024), 2),
                        "num_files": book.get("num_files") or 1,
                        "cover_url": f"/api/bookorbit/audiobook-cover/{book_id}",
                    })
            except Exception as e:
                logger.warning(f"⚠️ Forge audio BookOrbit search failed: {e}", exc_info=True)

        all_audiobooks = get_audiobooks_conditionally()

        for ab in all_audiobooks:
            if audiobook_matches_search(ab, query_lower):
                item_details = clients.abs_client.get_item_details(ab.get('id'))
                if not item_details:
                    continue

                media = item_details.get('media', {})
                metadata = media.get('metadata', {})
                audio_files = media.get('audioFiles', [])
                title = metadata.get('title', ab.get('name', 'Unknown'))

                if not audio_files:
                    continue

                size_mb = sum(f.get('metadata', {}).get('size', 0) for f in audio_files) / (1024 * 1024)

                # Build cover URL
                cover_url = ""
                abs_server = os.environ.get("ABS_SERVER", "")
                if abs_server:
                    cover_url = f"/api/cover-proxy/{ab.get('id')}"

                if str(ab.get("id")) in found_ids:
                    continue
                found_ids.add(str(ab.get("id")))
                results.append({
                    "id": ab.get("id"),
                    "audio_source": "ABS",
                    "audio_source_id": ab.get("id"),
                    "title": title,
                    "author": metadata.get('authorName') or get_abs_author(ab),
                    "file_size_mb": round(size_mb, 2),
                    "num_files": len(audio_files),
                    "cover_url": cover_url,
                })

        return jsonify(results)
    except Exception as e:
        logger.error(f"❌ Forge audio search failed: {e}", exc_info=True)
        return jsonify([])


def forge_search_text():
    """API: Unified text source search for Forge - ABS ebooks, Grimmory, CWA, local files."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])

    results = []
    found_ids = set()  # Dedupe
    query_lower = query.lower()
    clients = uc()  # per-user client bundle (their library/sources), else global

    # 1. Grimmory
    if clients.booklore_client.is_configured():
        try:
            books = clients.booklore_client.search_books(query)
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if fname.lower().endswith('.epub'):
                        key = f"booklore_{b.get('id', fname)}"
                        if key not in found_ids:
                            found_ids.add(key)
                            results.append({
                                "id": key,
                                "title": b.get('title', fname),
                                "author": b.get('authors', ''),
                                "source": "Grimmory",
                                "filename": fname,
                                "booklore_id": b.get('id'),
                            })
        except Exception as e:
            logger.warning(f"⚠️ Forge: Grimmory search failed: {e}", exc_info=True)

    # 2. BookOrbit
    try:
        bookorbit_client = clients.bookorbit_client
        if bookorbit_client and bookorbit_client.is_configured():
            bo_books = bookorbit_client.search_ebooks(query)
            if bo_books:
                for b in bo_books:
                    fname = b.get('fileName') or ''
                    ext = (b.get('primaryFormat') or Path(fname).suffix.lstrip('.') or 'epub').lower()
                    if ext != 'epub' and fname and not fname.lower().endswith('.epub'):
                        continue
                    key = f"bookorbit_{b.get('id', fname)}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": b.get('title', fname or 'Unknown'),
                            "author": _coerce_author_display(b.get('authors')),
                            "source": "BookOrbit",
                            "filename": fname,
                            "bookorbit_id": b.get('id'),
                            "source_id": b.get('id'),
                        })
    except Exception as e:
        logger.warning(f"⚠️ Forge: BookOrbit search failed: {e}", exc_info=True)

    # 2b. Kavita
    try:
        kavita_client = getattr(clients, "kavita_client", None)
        if kavita_client and kavita_client.is_configured():
            for book in kavita_client.search_ebooks(query) or []:
                filename = book.get('fileName') or book.get('filename') or ''
                if not filename.lower().endswith('.epub'):
                    continue
                key = f"kavita_{book.get('id', filename)}"
                if key in found_ids:
                    continue
                found_ids.add(key)
                results.append({
                    "id": key,
                    "title": book.get('title') or filename or 'Unknown',
                    "author": _coerce_author_display(book.get('authors') or book.get('author')),
                    "source": "Kavita",
                    "filename": filename,
                    "kavita_id": book.get('id'),
                    "source_id": book.get('id'),
                })
    except Exception as e:
        logger.warning("Forge: Kavita search failed: %s", e, exc_info=True)

    # 2c. BookFusion
    try:
        bookfusion_client = clients.bookfusion_client
        if bookfusion_client and bookfusion_client.is_configured():
            bf_books = bookfusion_client.search_books(page=1, per_page=50, q=query)
            for b in bf_books or []:
                bf_id = b.get("id") or b.get("book_id")
                if bf_id in (None, ""):
                    continue
                title = str(b.get("title") or b.get("name") or f"BookFusion {bf_id}").strip()
                author = _coerce_author_display(b.get("authors") or b.get("author"))
                if query_lower and query_lower not in f"{title} {author}".lower():
                    continue
                key = f"bookfusion_{bf_id}"
                if key not in found_ids:
                    found_ids.add(key)
                    results.append({
                        "id": key,
                        "title": title,
                        "author": author,
                        "source": "BookFusion",
                        "filename": f"bookfusion_{bf_id}.epub",
                        "bookfusion_id": bf_id,
                        "source_id": bf_id,
                    })
    except Exception as e:
        logger.warning(f"⚠️ Forge: BookFusion search failed: {e}", exc_info=True)

    # 3. ABS Ebooks
    try:
        abs_client = clients.abs_client
        if abs_client:
            abs_ebooks = abs_client.search_ebooks(query)
            if abs_ebooks:
                for ab in abs_ebooks:
                    ebook_files = abs_client.get_ebook_files(ab['id'])
                    if ebook_files:
                        ef = ebook_files[0]
                        key = f"abs_{ab['id']}"
                        if key not in found_ids:
                            found_ids.add(key)
                            results.append({
                                "id": key,
                                "title": ab.get('title', 'Unknown'),
                                "author": ab.get('author', ''),
                                "source": "ABS",
                                "abs_id": ab['id'],
                                "ext": ef.get('ext', 'epub'),
                            })
    except Exception as e:
        logger.warning(f"⚠️ Forge: ABS ebook search failed: {e}", exc_info=True)

    # 4. CWA
    try:
        library_service = clients.library_service
        if library_service and library_service.cwa_client and library_service.cwa_client.is_configured():
            cwa_results = library_service.cwa_client.search_ebooks(query)
            if cwa_results:
                for cr in cwa_results:
                    key = f"cwa_{cr.get('id', 'unknown')}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": cr.get('title', 'Unknown'),
                            "author": cr.get('author', ''),
                            "source": "CWA",
                            "cwa_id": cr.get('id'),
                            "ext": cr.get('ext', 'epub'),
                            "download_url": cr.get('download_url', ''),
                        })
    except Exception as e:
        logger.warning(f"⚠️ Forge: CWA search failed: {e}", exc_info=True)

    # 5. Local files from BOOKS_DIR
    try:
        local_books_dir = Path(os.environ.get("BOOKS_DIR", "/books"))
        if local_books_dir.exists():
            for epub in local_books_dir.rglob("*.epub"):
                if "(readaloud)" in epub.name.lower():
                    continue
                if query_lower in epub.name.lower():
                    key = f"local_{epub.name}"
                    if key not in found_ids:
                        found_ids.add(key)
                        results.append({
                            "id": key,
                            "title": epub.stem,
                            "author": "",
                            "source": "Local File",
                            "path": str(epub),
                            "file_size_mb": round(epub.stat().st_size / (1024 * 1024), 2),
                        })
    except Exception as e:
        logger.warning(f"⚠️ Forge: Local file search failed: {e}", exc_info=True)

    return jsonify(results)





def forge_process():
    """API: Start the forge process in the background."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON payload"}), 400

    requested_abs_id = data.get('abs_id')
    _requested_key = str(requested_abs_id or '')
    _inferred_source = next(
        (src for src in _LIBRARY_AUDIO_SOURCES if _requested_key.startswith(f"{src.lower()}:")),
        'ABS',
    )
    audio_source = (data.get('audio_source') or _inferred_source).strip()
    audio_source_id = str(data.get('audio_source_id') or requested_abs_id or '').strip()
    if audio_source in _LIBRARY_AUDIO_SOURCES and audio_source_id.lower().startswith(f"{audio_source.lower()}:"):
        audio_source_id = audio_source_id.split(":", 1)[1].strip()
    text_item = data.get('text_item')
    forge_stage_mode = data.get('forge_stage_mode')

    if not text_item:
        return jsonify({"error": "Missing text_item"}), 400
    # text_item is client-supplied: a filesystem path must name a file in the library,
    # never an arbitrary container path whose bytes would be uploaded upstream. A URL
    # (CWA carries its download_url here) is not a local path and is left alone.
    if isinstance(text_item, dict):
        raw_path = str(text_item.get('path') or '').strip()
        if raw_path and "://" not in raw_path:
            safe_path = _safe_local_source_path(raw_path)
            if not safe_path:
                return jsonify({"error": "Invalid local file path"}), 400
            text_item['path'] = safe_path
    if audio_source == "ABS" and not requested_abs_id:
        return jsonify({"error": "Missing abs_id"}), 400
    if audio_source in _LIBRARY_AUDIO_SOURCES and not audio_source_id:
        return jsonify({"error": "Missing audio_source_id"}), 400

    abs_id = requested_abs_id if audio_source == "ABS" else _build_bridge_key(audio_source, audio_source_id)
    clients = uc()
    if not clients.storyteller_client.is_configured():
        return jsonify({"error": "Storyteller is not configured"}), 409

    # Get title/author from the audio provider for folder naming
    title = "Unknown"
    author = "Unknown"
    try:
        if audio_source == "BookLore":
            book_detail = clients.booklore_client.get_book_by_id(audio_source_id)
            if book_detail:
                metadata = book_detail.get("metadata") or {}
                title = (
                    metadata.get("title")
                    or book_detail.get("title")
                    or book_detail.get("fileName")
                    or f"Grimmory {audio_source_id}"
                )
                author = (
                    _coerce_author_display(book_detail.get("authors"))
                    or _coerce_author_display(metadata.get("authors"))
                    or "Unknown"
                )
        elif audio_source == "BookOrbit":
            book_detail = clients.bookorbit_client.get_book_by_id(audio_source_id)
            if book_detail:
                title = book_detail.get("title") or f"BookOrbit {audio_source_id}"
                author = _coerce_author_display(book_detail.get("authors")) or "Unknown"
        else:
            item_details = clients.abs_client.get_item_details(abs_id)
            if item_details:
                metadata = item_details.get('media', {}).get('metadata', {})
                title = metadata.get('title', 'Unknown')
                author = metadata.get('authorName', '') or get_abs_author(item_details) or 'Unknown'
    except Exception as e:
        logger.warning(f"⚠️ Forge: Could not get audio metadata for '{abs_id}': {e}", exc_info=True)

    # Start manual forge in service
    try:
        forge_kwargs = {}
        if audio_source in _LIBRARY_AUDIO_SOURCES:
            forge_kwargs["audio_source"] = audio_source
            forge_kwargs["audio_source_id"] = audio_source_id
        if forge_stage_mode:
            forge_kwargs["stage_mode"] = forge_stage_mode

        if forge_kwargs:
            container.forge_service().start_manual_forge(
                abs_id,
                text_item,
                title,
                author,
                **forge_kwargs,
                **_client_bundle_kwargs(clients),
            )
        else:
            container.forge_service().start_manual_forge(
                abs_id, text_item, title, author, **_client_bundle_kwargs(clients)
            )
        msg = (
            f"Forge started for '{title}'. Processing and staged-source cleanup are running in background."
            if str(forge_stage_mode or "").strip().lower() != "hardlink"
            else f"Forge started for '{title}'. Processing is running in background and staged sources will be kept."
        )
    except Exception as e:
        logger.error(f"❌ Failed to start forge: {e}", exc_info=True)
        return jsonify({"error": f"Failed to start forge: {e}"}), 500

    return jsonify({
        "message": msg,
        "title": title,
        "author": author,
    }), 202


def alignments_llm_status():
    """API: Report how each stored alignment map was built (which used the LLM)."""
    try:
        # Self-heal legacy maps: classify NULL provenance by map shape (no re-transcription)
        # so the report and the re-align target list are accurate.
        database_service.backfill_alignment_methods()
        # Score maps stored before quality tracking existed, a bounded batch per call
        # so the health panel's scores fill in over a few page loads (issue #426 phase 4).
        database_service.backfill_alignment_quality()
        return jsonify(database_service.get_alignment_provenance())
    except Exception as e:
        logger.error(f"❌ Failed to read alignment provenance: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def alignments_realign():
    """API: Queue alignment maps for re-processing under the LLM-enabled pipeline.

    Body: {"abs_id": "..."} for one book, or {"scope": "all_non_llm"} to queue every
    pre-LLM/linear map. Sets the books' status to 'pending' so the forge pipeline
    rebuilds them on the next cycle.
    """
    data = request.get_json(silent=True) or {}
    abs_id = (data.get("abs_id") or "").strip()
    scope = (data.get("scope") or "").strip()

    try:
        if abs_id:
            targets = [abs_id]
        elif scope == "all_non_llm":
            targets = database_service.get_books_needing_llm_realign()
        else:
            return jsonify({"error": "Provide 'abs_id' or scope 'all_non_llm'"}), 400

        queued = 0
        for target in targets:
            if database_service.set_book_status(target, "pending"):
                queued += 1
        logger.info(f"🔁 Re-align queued {queued} book(s) (scope='{scope or 'single'}')")
        return jsonify({"queued": queued})
    except Exception as e:
        logger.error(f"❌ Failed to queue re-align: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def alignments_restore():
    """API: Restore a book's alignment map to the backup taken before its last
    overwrite (issue #426 phase 4).

    Body: {"abs_id": "..."}. Returns {"restored": bool} — False when there is no
    backup on file for that book.
    """
    data = request.get_json(silent=True) or {}
    abs_id = (data.get("abs_id") or "").strip()
    if not abs_id:
        return jsonify({"error": "Provide 'abs_id'"}), 400

    try:
        alignment_service = getattr(manager, "alignment_service", None) if manager else None
        restored = bool(alignment_service and alignment_service.restore_previous_alignment(abs_id))
        return jsonify({"restored": restored})
    except Exception as e:
        logger.error(f"❌ Failed to restore alignment for {abs_id}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def match():
    if request.method == 'GET':
        search = request.args.get('search', '')
        return redirect(url_for('add_book', search=search), code=302)

    if request.method == 'POST':
        abs_id = (request.form.get('audiobook_id') or '').strip()
        audio_source = (request.form.get('audio_source') or ('ABS' if abs_id else '')).strip() or None
        audio_source_id = (request.form.get('audio_source_id') or abs_id).strip() or None
        audio_title = (request.form.get('audio_title') or '').strip() or None
        audio_cover_url = (request.form.get('audio_cover_url') or '').strip() or None
        audio_provider_book_id = (request.form.get('audio_provider_book_id') or audio_source_id or '').strip() or None
        audio_provider_file_id = (request.form.get('audio_provider_file_id') or '').strip() or None
        audio_duration = _parse_audio_duration(request.form.get('audio_duration'))
        selected_filename = (request.form.get('ebook_filename') or '').strip() or None
        ebook_source = (request.form.get('ebook_source') or request.form.get('source_type') or '').strip() or None
        ebook_source_id = (request.form.get('ebook_source_id') or request.form.get('source_id') or '').strip() or None
        source_path = (request.form.get('ebook_source_path') or request.form.get('source_path') or '').strip() or None
        storyteller_uuid = (request.form.get('storyteller_uuid') or '').strip() or None
        forge_stage_mode = (request.form.get('forge_stage_mode') or '').strip() or None
        ebook_filename = selected_filename
        original_ebook_filename = selected_filename
        clients = uc()
        if request.form.get('action') == 'forge_match' and not clients.storyteller_client.is_configured():
            return "Storyteller is not configured", 409
        audiobooks = get_audiobooks_conditionally()
        selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None) if abs_id else None

        if request.form.get('action') == 'forge_match' and audio_source not in ('ABS',) + _LIBRARY_AUDIO_SOURCES:
            return "Forge match requires an ABS, Grimmory or BookOrbit audiobook", 400

        if request.form.get('action') == 'forge_match' and audio_source == 'ABS' and not selected_ab:
            return "Audiobook not found", 404

        if _normalize_text_source_type(ebook_source) == "BookFusion" and ebook_source_id and request.form.get('action') != 'forge_match':
            if audio_source not in ('ABS', *_LIBRARY_AUDIO_SOURCES):
                return "BookFusion linking requires an audiobook", 400
            if audio_source == 'ABS' and not selected_ab:
                return "Audiobook not found", 404
            # Download BookFusion's EPUB into the epub cache so the link flows
            # through the normal ebook match path — hash → pending → forge/Whisper
            # → Hardcover/StoryGraph, tri-linking with any selected Storyteller
            # readalong — exactly like every other ebook source, instead of the
            # progress-only dead end that skipped transcription and tracker matching.
            bf_cached = _ensure_bookfusion_ebook_cached(ebook_source_id)
            if not bf_cached:
                return "Could not download the BookFusion book for linking", 502
            if not selected_filename:
                selected_filename = bf_cached
                ebook_filename = bf_cached
                original_ebook_filename = bf_cached
            if audio_source in _LIBRARY_AUDIO_SOURCES:
                bf_title = request.form.get('ebook_display_name') or Path(selected_filename or f"bookfusion_{ebook_source_id}").stem
                saved_book, err_msg, err_code = _create_or_update_bookfusion_progress_mapping(
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    audio_title=audio_title or bf_title,
                    audio_cover_url=audio_cover_url,
                    audio_duration=audio_duration,
                    audio_provider_book_id=audio_provider_book_id,
                    audio_provider_file_id=audio_provider_file_id,
                    bookfusion_id=ebook_source_id,
                    bookfusion_title=bf_title,
                    storyteller_uuid=storyteller_uuid,
                )
                if err_msg:
                    return err_msg, err_code
                if saved_book:
                    _claim_book_for_current_user(saved_book.abs_id)
                return redirect(url_for('index'))
            # audio_source == 'ABS': fall through to the standard ABS + ebook match
            # path below (Storyteller tri-link, forge/Whisper, tracker auto-match).

        audio_only = (request.form.get("audio_only") or "").strip().lower() in (
            "true", "1", "yes", "on"
        )
        if audio_only and request.form.get('action') != 'forge_match':
            if selected_filename or storyteller_uuid:
                return "Audio-only mappings cannot include a text source", 400
            if not audio_source or not audio_source_id:
                return "Please select an audiobook for an audio-only mapping", 400
            if audio_source == "ABS":
                if not selected_ab:
                    return "Audiobook not found", 404
                if not audio_title:
                    audio_title = manager.get_abs_title(selected_ab)
                if audio_duration is None:
                    audio_duration = manager.get_duration(selected_ab)

            saved_book, err_msg, err_code = _create_or_update_audio_only_mapping(
                audio_source=audio_source,
                audio_source_id=audio_source_id,
                audio_title=audio_title,
                audio_cover_url=audio_cover_url,
                audio_duration=audio_duration,
                audio_provider_book_id=audio_provider_book_id,
                audio_provider_file_id=audio_provider_file_id,
            )
            if err_msg:
                return err_msg, err_code
            if saved_book:
                _claim_book_for_current_user(saved_book.abs_id)
            return redirect(url_for('index'))

        if audio_source in _LIBRARY_AUDIO_SOURCES and audio_source_id and request.form.get('action') != 'forge_match':
            saved_book, err_msg, err_code = _create_or_update_library_audio_mapping(
                audio_source=audio_source,
                audio_source_id=audio_source_id,
                audio_title=audio_title or Path(selected_filename or f"{audio_source.lower()}_{audio_source_id}").stem,
                audio_cover_url=audio_cover_url,
                audio_duration=audio_duration,
                audio_provider_book_id=audio_provider_book_id,
                audio_provider_file_id=audio_provider_file_id,
                ebook_filename=selected_filename,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id,
                storyteller_uuid=storyteller_uuid,
                ebook_source_path=source_path,
            )
            if err_msg:
                return err_msg, err_code
            if saved_book:
                _claim_book_for_current_user(saved_book.abs_id)
            return redirect(url_for('index'))

        if not selected_ab and request.form.get('action') != 'forge_match':
            if not (storyteller_uuid or selected_filename):
                return "Please select a text source (Storyteller or Standard Ebook)", 400

            storyteller_meta = _get_storyteller_display_metadata(storyteller_uuid)
            ebook_only_title = None
            if ebook_source == 'ABS' and ebook_source_id:
                try:
                    item_details = clients.abs_client.get_item_details(ebook_source_id)
                except Exception as e:
                    logger.warning(
                        "Match: failed ABS ebook metadata lookup for '%s': %s",
                        sanitize_log_data(ebook_source_id),
                        e,
                        exc_info=True,
                    )
                    item_details = None
                metadata = (item_details or {}).get('media', {}).get('metadata', {})
                ebook_only_title = (metadata.get('title') or '').strip() or None
            if not ebook_only_title:
                ebook_only_title = (
                    Path(selected_filename).stem
                    if selected_filename
                    else (storyteller_meta.get("title") or f"storyteller_{storyteller_uuid or 'book'}")
                )
            logger.info(
                "Match: entering ebook-only create path (storyteller_selected=%s, ebook_selected=%s)",
                bool(storyteller_uuid),
                bool(selected_filename),
            )
            saved_book, err_msg, err_code = _upsert_storyteller_mapping(
                mode_hint="ebook_only_create",
                abs_id=ebook_source_id if ebook_source == 'ABS' and ebook_source_id else None,
                abs_title=ebook_only_title,
                storyteller_uuid=storyteller_uuid,
                ebook_filename=selected_filename,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id,
                duration=0.0,
            )
            if err_msg:
                return err_msg, err_code
            logger.info("Match: ebook-only mapping ready for '%s'", sanitize_log_data(saved_book.abs_id))
            if saved_book:
                _claim_book_for_current_user(saved_book.abs_id)
                _shelve_saved_ebook(saved_book)
            return redirect(url_for('index'))

        # [NEW ACTION] Forge & Match (supports both ABS and Grimmory audiobooks)
        if request.form.get('action') == 'forge_match':
            original_filename = request.form.get('ebook_filename')
            if not original_filename:
                return "Original ebook filename required for forge match", 400

            source_type = request.form.get('source_type')
            source_path = request.form.get('source_path') or source_path
            source_id = request.form.get('source_id')
            text_item = _build_forge_text_item(source_type, source_id, source_path, original_filename)
            normalized_source_type = text_item.get("source")

            initial_booklore_id = source_id if normalized_source_type == 'Booklore' else None
            kosync_doc_id = get_kosync_id_for_ebook(
                original_filename,
                initial_booklore_id,
                bookorbit_id=source_id if normalized_source_type == 'BookOrbit' else None,
                kavita_id=source_id if normalized_source_type == 'Kavita' else None,
                source_path=source_path,
            )

            if not kosync_doc_id:
                logger.warning(f"Could not compute ID for original '{original_filename}'")

            from src.db.models import Book

            if audio_source in _LIBRARY_AUDIO_SOURCES:
                forge_title = audio_title or Path(selected_filename or f"{audio_source.lower()}_{audio_source_id}").stem
                forge_id = _build_bridge_key(audio_source, audio_source_id)
                book = Book(
                    abs_id=forge_id,
                    abs_title=forge_title,
                    ebook_filename=original_filename,
                    original_ebook_filename=original_filename,
                    kosync_doc_id=kosync_doc_id or f"forging_{forge_id}",
                    status="forging",
                    duration=audio_duration or 0.0,
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    audio_provider_book_id=audio_provider_book_id,
                    audio_provider_file_id=audio_provider_file_id,
                    audio_title=forge_title,
                    audio_cover_url=audio_cover_url,
                    audio_duration=audio_duration,
                    ebook_source=normalized_source_type or None,
                    ebook_source_id=(source_id or '').strip() or None,
                )
                database_service.save_book(book)
                _record_forge_match_job(forge_id, progress=0.02, last_error="Queued Forge & Match")

                container.forge_service().start_auto_forge_match(
                    abs_id=forge_id,
                    text_item=text_item,
                    title=forge_title,
                    author=None,
                    original_filename=original_filename,
                    original_hash=kosync_doc_id,
                    audio_source=audio_source,
                    audio_source_id=audio_source_id,
                    **({"stage_mode": forge_stage_mode} if forge_stage_mode else {}),
                    **_client_bundle_kwargs(clients),
                )
            else:
                abs_title = manager.get_abs_title(selected_ab)
                book = Book(
                    abs_id=abs_id,
                    abs_title=abs_title,
                    ebook_filename=original_filename,
                    original_ebook_filename=original_filename,
                    kosync_doc_id=kosync_doc_id or f"forging_{abs_id}",
                    status="forging",
                    duration=manager.get_duration(selected_ab),
                    ebook_source=normalized_source_type or None,
                    ebook_source_id=(source_id or '').strip() or None,
                )
                database_service.save_book(book)
                _record_forge_match_job(abs_id, progress=0.02, last_error="Queued Forge & Match")

                author = get_abs_author(selected_ab)
                container.forge_service().start_auto_forge_match(
                    abs_id=abs_id,
                    text_item=text_item,
                    title=abs_title,
                    author=author,
                    original_filename=original_filename,
                    original_hash=kosync_doc_id,
                    **({"stage_mode": forge_stage_mode} if forge_stage_mode else {}),
                    **_client_bundle_kwargs(clients),
                )

            forge_book_id = forge_id if audio_source in _LIBRARY_AUDIO_SOURCES else abs_id
            database_service.dismiss_suggestion(forge_book_id)
            if kosync_doc_id:
                database_service.dismiss_suggestion(kosync_doc_id)

            _claim_book_for_current_user(forge_book_id)
            return redirect(url_for('index'))

        if not selected_ab:
            return "Audiobook not found", 404

        abs_title = manager.get_abs_title(selected_ab)
        item_details = clients.abs_client.get_item_details(abs_id)
        chapters = item_details.get('media', {}).get('chapters', []) if item_details else []

        booklore_id = None
            
        # Storyteller tri-link logic
        if storyteller_uuid:
            # If Storyteller UUID is selected, we prioritize it
            try:
                logger.info(f"🔍 Using Storyteller Artifact: '{storyteller_uuid}'")
                target_filename, _target_path = _download_storyteller_artifact(
                    storyteller_uuid,
                    abs_title,
                    original_ebook_filename=selected_filename,
                )
                if not target_filename:
                    return "Failed to download Storyteller artifact", 500

                ebook_filename = target_filename
                original_ebook_filename = selected_filename

                kosync_doc_id = _compute_storyteller_trilink_kosync_id(
                    original_ebook_filename,
                    target_filename,
                    "Tri-Link",
                )
                    
            except Exception as e:
                logger.error(f"❌ Storyteller Link failed: {e}", exc_info=True)
                return f"Storyteller Link failed: {e}", 500
        else:
            # Fallback to Standard Logic
            if clients.booklore_client.is_configured():
                book = clients.booklore_client.find_book_by_filename(ebook_filename)
                if book:
                    booklore_id = book.get('id')

            # Compute KOSync ID (Grimmory API first, filesystem fallback)
            kosync_doc_id = get_kosync_id_for_ebook(
                ebook_filename,
                booklore_id,
                bookorbit_id=ebook_source_id if ebook_source == 'BookOrbit' else None,
                kavita_id=ebook_source_id if ebook_source == 'Kavita' else None,
                source_path=source_path,
            )

        if not kosync_doc_id:
            logger.warning(f"⚠️ Cannot compute KOSync ID for '{sanitize_log_data(ebook_filename)}': File not found in Grimmory or filesystem")
            return "Could not compute KOSync ID for ebook", 404

        # Hash Preservation: If the book already has a kosync_doc_id set,
        # preserve it. This respects manual overrides via update_hash and
        # prevents re-match from reverting a user's custom hash.
        current_book_entry = database_service.get_book(abs_id)
        if current_book_entry and current_book_entry.kosync_doc_id:
            logger.info(f"🔄 Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{abs_id}' instead of new hash '{kosync_doc_id}'")
            kosync_doc_id = current_book_entry.kosync_doc_id
        if current_book_entry and not original_ebook_filename:
            original_ebook_filename = current_book_entry.original_ebook_filename

        # [DUPLICATE MERGE] Check if this ebook is already linked to another ABS ID (e.g. ebook-only entry)
        existing_book = database_service.get_book_by_kosync_id(kosync_doc_id)
        migration_source_id = None
        abs_ebook_item_id = None
        preserved_storyteller_uuid = current_book_entry.storyteller_uuid if current_book_entry else None
        preserved_transcript_source = current_book_entry.transcript_source if current_book_entry else None
        preserved_transcript_file = current_book_entry.transcript_file if current_book_entry else None
        preserved_original_ebook_filename = current_book_entry.original_ebook_filename if current_book_entry else None
        preserved_abs_ebook_item_id = current_book_entry.abs_ebook_item_id if current_book_entry else None

        if existing_book and existing_book.abs_id != abs_id:
            logger.info(f"🔄 Found existing book entry '{existing_book.abs_id}' for this ebook — Merging into '{abs_id}'")
            migration_source_id = existing_book.abs_id
            abs_ebook_item_id = existing_book.abs_ebook_item_id or existing_book.abs_id
            preserved_storyteller_uuid = existing_book.storyteller_uuid or preserved_storyteller_uuid
            preserved_transcript_source = existing_book.transcript_source or preserved_transcript_source
            preserved_transcript_file = existing_book.transcript_file or preserved_transcript_file
            preserved_original_ebook_filename = existing_book.original_ebook_filename or preserved_original_ebook_filename
            preserved_abs_ebook_item_id = existing_book.abs_ebook_item_id or preserved_abs_ebook_item_id
            logger.info(
                "Match merge: preserving storyteller metadata from '%s' -> '%s' (uuid=%s, transcript=%s)",
                sanitize_log_data(existing_book.abs_id),
                sanitize_log_data(abs_id),
                bool(preserved_storyteller_uuid),
                bool(preserved_transcript_file),
            )

        if not original_ebook_filename:
            original_ebook_filename = preserved_original_ebook_filename
        if not original_ebook_filename and existing_book:
            original_ebook_filename = existing_book.original_ebook_filename or existing_book.ebook_filename
        if abs_ebook_item_id is None:
            abs_ebook_item_id = preserved_abs_ebook_item_id
        if abs_ebook_item_id is None and current_book_entry:
            abs_ebook_item_id = current_book_entry.abs_ebook_item_id

        # Extract series metadata from ABS item details
        _match_series_name, _match_series_seq = None, None
        if item_details:
            _abs_meta = item_details.get("media", {}).get("metadata", {})
            _match_series_name, _match_series_seq = _extract_series_from_abs_metadata(_abs_meta)

        # Create Book object and save to database service
        from src.db.models import Book
        storyteller_manifest = ingest_storyteller_transcripts(abs_id, abs_title, chapters)
        effective_storyteller_uuid = storyteller_uuid or preserved_storyteller_uuid
        transcript_source = (
            _storyteller_transcript_source(effective_storyteller_uuid, storyteller_manifest)
            or preserved_transcript_source
        )
        transcript_file = storyteller_manifest or preserved_transcript_file
        # The replacement Book below overwrites the stored mapping wholesale, so the
        # guard has to weigh the identity it is about to lose while it is still on
        # disk. Without this, re-matching an already-aligned book queued it for
        # transcription again — the exact regression the guard was written to stop.
        resolved_status = "pending"
        if current_book_entry is not None:
            _preserve_or_reset_mapping_status(
                current_book_entry,
                kosync_doc_id=kosync_doc_id,
                ebook_filename=ebook_filename,
                audio_source_id=abs_id,
                storyteller_uuid=effective_storyteller_uuid,
                ebook_source=ebook_source,
                ebook_source_id=ebook_source_id,
            )
            resolved_status = current_book_entry.status or "pending"
        book = Book(
            abs_id=abs_id,
            abs_title=abs_title,
            audio_source="ABS",
            audio_source_id=abs_id,
            audio_title=abs_title,
            audio_cover_url=f"/api/cover-proxy/{abs_id}",
            audio_duration=manager.get_duration(selected_ab),
            audio_provider_book_id=abs_id,
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=transcript_file,
            status=resolved_status,
            duration=manager.get_duration(selected_ab),
            transcript_source=transcript_source,
            storyteller_uuid=effective_storyteller_uuid,
            original_ebook_filename=original_ebook_filename,
            abs_ebook_item_id=abs_ebook_item_id,
            ebook_source=ebook_source,
            ebook_source_id=ebook_source_id,
            series_name=_match_series_name,
            series_sequence=_match_series_seq,
        )

        database_service.save_book(book)
        _claim_book_for_current_user(abs_id)

        # [DUPLICATE MERGE] Perform Migration if needed
        if migration_source_id:
            try:
                database_service.migrate_book_data(migration_source_id, abs_id)
                database_service.delete_book(migration_source_id)
                logger.info(f"✅ Successfully merged {migration_source_id} into {abs_id}")
            except Exception as e:
                logger.error(f"❌ Failed to merge book data: {e}", exc_info=True)

        # Trigger Hardcover/StoryGraph automatch in the background (redirect now).
        _enqueue_tracker_automatch(clients.sync_clients, book)

        # Record the per-user BookFusion link so progress + annotation sync resolve
        # the remote book (the EPUB was cached above as original_ebook_filename).
        if _normalize_text_source_type(ebook_source) == "BookFusion" and ebook_source_id:
            _persist_bookfusion_link_for_current_user(
                abs_id, "BookFusion", ebook_source_id, title=abs_title, author=None,
            )

        if not str(abs_id).startswith('booklore:'):
            clients.abs_client.add_to_collection(abs_id, user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))
        # Use original filename for shelf if we switched to storyteller
        shelf_filename = original_ebook_filename or ebook_filename
        if shelf_filename and not _is_storyteller_artifact_filename(shelf_filename):
            _shelve_matched_ebook(shelf_filename, getattr(book, "ebook_source", None),
                                  getattr(book, "ebook_source_id", None))
        if clients.storyteller_client.is_configured():
            if book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

        # Auto-dismiss any pending suggestion for this book
        # Need to dismiss by BOTH abs_id (audiobook-triggered) and kosync_doc_id (ebook-triggered)
        database_service.dismiss_suggestion(abs_id)
        database_service.dismiss_suggestion(kosync_doc_id)
        _adopt_kosync_progress_for_book(abs_id, kosync_doc_id)
        
        # Check for a different hash for this filename, such as one reported by a device.
        try:
            device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                logger.info(f"🔄 Dismissing additional suggestion/hash for '{ebook_filename}': '{device_doc.document_hash}'")
                database_service.dismiss_suggestion(device_doc.document_hash)
                if not device_doc.linked_abs_id:
                    # A device-served build of the same book carries its own hash and its
                    # own stored progress, so adopt it as a durable sibling. Only when it
                    # is unclaimed -- a hash already pointing at another book is never
                    # re-pointed from here.
                    database_service.link_kosync_document(device_doc.document_hash, abs_id)
                    logger.info(f"🔗 Linked device hash '{device_doc.document_hash}' to '{abs_id}'")
        except Exception as e:
            logger.warning(f"⚠️ Failed to check/dismiss device hash: {e}", exc_info=True)

        return redirect(url_for('index'))

def _create_ebook_only_mapping_from_queue_item(item):
    """Create an ebook-only / storyteller-only mapping (no audio) from a queue item.

    Mirrors the match() ebook-only-create path. Used by the batch processors when a
    queued Add Book item has no audio source (the matrix says: no audio → match only).
    """
    storyteller_uuid = (item.get('storyteller_uuid') or '').strip() or None
    ebook_filename = (item.get('ebook_filename') or '').strip() or None
    ebook_source = item.get('ebook_source')
    ebook_source_id = item.get('ebook_source_id')
    if _normalize_text_source_type(ebook_source) == "BookFusion":
        logger.warning(
            "⚠️ Add Book (ebook-only) skipped '%s': BookFusion links require an audio mapping",
            sanitize_log_data(item.get('abs_title') or ebook_source_id or ebook_filename),
        )
        return
    if not (storyteller_uuid or ebook_filename):
        return
    title = (
        item.get('abs_title')
        or (Path(ebook_filename).stem if ebook_filename else None)
        or f"storyteller_{storyteller_uuid or 'book'}"
    )
    saved_book, err_msg, _err_code = _upsert_storyteller_mapping(
        mode_hint="ebook_only_create",
        abs_id=ebook_source_id if ebook_source == 'ABS' and ebook_source_id else None,
        abs_title=title,
        storyteller_uuid=storyteller_uuid,
        ebook_filename=ebook_filename,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
        duration=0.0,
    )
    if err_msg:
        logger.warning("⚠️ Add Book (ebook-only) skipped '%s': %s", sanitize_log_data(title), err_msg)
    elif saved_book:
        _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
    return saved_book if not err_msg else None


def _create_audio_only_mapping_from_queue_item(item):
    """Create an audio-only mapping from a queued audiobook selection."""
    audio_source = (item.get("audio_source") or "").strip() or None
    audio_source_id = (item.get("audio_source_id") or item.get("abs_id") or "").strip()
    if not audio_source or not audio_source_id:
        return

    saved_book, err_msg, _err_code = _create_or_update_audio_only_mapping(
        audio_source=audio_source,
        audio_source_id=audio_source_id,
        audio_title=item.get("audio_title") or item.get("abs_title"),
        audio_cover_url=item.get("audio_cover_url") or item.get("cover_url"),
        audio_duration=_parse_audio_duration(item.get("audio_duration") or item.get("duration")),
        audio_provider_book_id=item.get("audio_provider_book_id"),
        audio_provider_file_id=item.get("audio_provider_file_id"),
    )
    if err_msg:
        logger.warning(
            "⚠️ Add Book (audio-only) skipped '%s': %s",
            sanitize_log_data(item.get("abs_title") or audio_source_id),
            err_msg,
        )
    elif saved_book:
        _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
    return saved_book if not err_msg else None


def _record_forge_match_job(abs_id: str, progress: float = 0.0, last_error: str = None):
    """Persist Forge & Match wait state so it survives refreshes and restarts."""
    if not abs_id:
        return None
    from src.db.models import Job
    try:
        return database_service.save_job(
            Job(
                abs_id=abs_id,
                last_attempt=time.time(),
                retry_count=0,
                last_error=last_error,
                progress=progress,
            )
        )
    except Exception as exc:
        logger.warning("Forge & Match: failed to record job state for '%s': %s", sanitize_log_data(abs_id), exc, exc_info=True)
        return None


def _process_forge_only_queue(queue_items, forge_stage_mode=None):
    """Background processor for the Add Book 'Forge only' action.

    Builds the Storyteller edition for each forge-eligible item (audio + a standard
    ebook) without creating a sync mapping — the same work as forge_process(), driven
    from queued items instead of a single JSON request.
    """
    clients = uc()
    for item in queue_items:
        if item.get('audio_only'):
            _create_audio_only_mapping_from_queue_item(item)
            continue
        audio_source = item.get('audio_source')
        if audio_source and _normalize_text_source_type(item.get('ebook_source')) == "BookFusion" and item.get('ebook_source_id'):
            saved_book, err_msg, _err_code = _create_or_update_bookfusion_progress_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id') or item.get('abs_id'),
                audio_title=item.get('audio_title') or item.get('abs_title'),
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=_parse_audio_duration(item.get('audio_duration') or item.get('duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                bookfusion_id=item.get('ebook_source_id'),
                bookfusion_title=item.get('ebook_display_name') or item.get('ebook_filename'),
                storyteller_uuid=item.get('storyteller_uuid'),
            )
            if err_msg:
                logger.warning(
                    "Forge only skipped BookFusion link for '%s': %s",
                    sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    err_msg,
                )
            elif saved_book:
                _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
                # This branch saved a mapping, so a shelf-watch approval behind it is
                # complete and its watch-shelf copy must move — same as the batch and
                # forge-match processors. The main forge path below creates no mapping
                # and leaves the suggestion pending, so it deliberately does not.
                _complete_shelf_watch_approval(_queue_item_shelf_watch_metadata(item))
            continue
        # Forge-only requires audio + a standard ebook (never a Storyteller edition).
        if not audio_source or item.get('storyteller_uuid') or not item.get('ebook_filename'):
            logger.info(
                "Forge only: skipping non-forge-eligible item '%s'",
                sanitize_log_data(item.get('abs_title') or item.get('abs_id')),
            )
            continue

        original_filename = (item.get('ebook_filename') or '').strip()
        source_type = _normalize_text_source_type(item.get('ebook_source'))
        source_id = str(item.get('ebook_source_id') or '').strip()
        source_path = str(item.get('ebook_source_path') or '').strip()
        if not source_type:
            source_type = 'Booklore' if source_id else 'Local File'
        if source_type == 'Local File' and not source_path:
            resolved_path = find_ebook_file(original_filename)
            source_path = str(resolved_path) if resolved_path else ''
        text_item = _build_forge_text_item(source_type, source_id, source_path, original_filename)

        if audio_source in _LIBRARY_AUDIO_SOURCES:
            audio_source_id = (item.get('audio_source_id') or '').strip()
            abs_id = _build_bridge_key(audio_source, audio_source_id)
        else:
            audio_source = 'ABS'
            abs_id = item.get('abs_id')
            audio_source_id = item.get('audio_source_id') or abs_id

        # Best-effort title/author for Storyteller folder naming (mirrors forge_process).
        title = item.get('audio_title') or 'Unknown'
        author = 'Unknown'
        try:
            if audio_source == 'BookLore':
                book_detail = clients.booklore_client.get_book_by_id(audio_source_id)
                if book_detail:
                    metadata = book_detail.get('metadata') or {}
                    title = metadata.get('title') or book_detail.get('title') or title
                    author = (
                        _coerce_author_display(book_detail.get('authors'))
                        or _coerce_author_display(metadata.get('authors'))
                        or author
                    )
            elif audio_source == 'BookOrbit':
                book_detail = clients.bookorbit_client.get_book_by_id(audio_source_id)
                if book_detail:
                    title = book_detail.get('title') or title
                    author = _coerce_author_display(book_detail.get('authors')) or author
            else:
                item_details = clients.abs_client.get_item_details(abs_id)
                if item_details:
                    metadata = item_details.get('media', {}).get('metadata', {})
                    title = metadata.get('title') or title
                    author = metadata.get('authorName', '') or get_abs_author(item_details) or author
        except Exception as e:
            logger.warning("Forge only: metadata lookup failed for '%s': %s", sanitize_log_data(abs_id), e, exc_info=True)

        forge_kwargs = {}
        if audio_source in _LIBRARY_AUDIO_SOURCES:
            forge_kwargs['audio_source'] = audio_source
            forge_kwargs['audio_source_id'] = audio_source_id
        if forge_stage_mode:
            forge_kwargs['stage_mode'] = forge_stage_mode
        try:
            container.forge_service().start_manual_forge(
                abs_id, text_item, title, author, **forge_kwargs, **_client_bundle_kwargs(clients)
            )
        except Exception as e:
            logger.error("❌ Forge only failed for '%s': %s", sanitize_log_data(title), e, exc_info=True)


def _process_batch_queue(queue_items):
    """Background processor for the batch-match 'Process Queue' action.

    Runs each item's onboarding (artifact download, hash, transcript ingest, save,
    collection/shelf) off the request thread via _spawn_user_background, so the page
    redirects immediately and books appear on the dashboard as each finishes.
    """
    from src.db.models import Book
    clients = uc()
    for item in queue_items:
        shelf_watch_meta = _queue_item_shelf_watch_metadata(item)
        if not item.get('audio_source'):
            # No audio: ebook-only / storyteller-only item — match only.
            saved_book = _create_ebook_only_mapping_from_queue_item(item)
            if saved_book and not _complete_shelf_watch_approval(shelf_watch_meta):
                _shelve_saved_ebook(saved_book)
            continue
        if item.get('audio_only'):
            if _create_audio_only_mapping_from_queue_item(item):
                _complete_shelf_watch_approval(shelf_watch_meta)
            continue
        audio_source = item.get('audio_source') or 'ABS'
        if _normalize_text_source_type(item.get('ebook_source')) == "BookFusion" and item.get('ebook_source_id'):
            saved_book, err_msg, _err_code = _create_or_update_bookfusion_progress_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id') or item.get('abs_id'),
                audio_title=item.get('audio_title') or item.get('abs_title'),
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=_parse_audio_duration(item.get('audio_duration') or item.get('duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                bookfusion_id=item.get('ebook_source_id'),
                bookfusion_title=item.get('ebook_display_name') or item.get('ebook_filename'),
                storyteller_uuid=item.get('storyteller_uuid'),
            )
            if err_msg:
                logger.warning(
                    "⚠️ Batch Match skipped BookFusion link for '%s': %s",
                    sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    err_msg,
                )
            elif saved_book:
                _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
                _complete_shelf_watch_approval(shelf_watch_meta)
            continue
        if audio_source in _LIBRARY_AUDIO_SOURCES:
            saved_book, err_msg, _err_code = _create_or_update_library_audio_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id'),
                audio_title=item.get('audio_title'),
                audio_cover_url=item.get('audio_cover_url'),
                audio_duration=_parse_audio_duration(item.get('audio_duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                ebook_filename=item.get('ebook_filename'),
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
                storyteller_uuid=item.get('storyteller_uuid'),
                ebook_source_path=item.get('ebook_source_path'),
            )
            if err_msg:
                logger.warning(
                    "⚠️ Batch Match skipped %s audiobook '%s': %s",
                    _audio_source_display_name(audio_source),
                    sanitize_log_data(item.get('audio_title') or item.get('audio_source_id')),
                    err_msg,
                )
            elif saved_book:
                _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
                _complete_shelf_watch_approval(shelf_watch_meta, remove_only=True)
            continue

        ebook_filename = item['ebook_filename']
        storyteller_uuid = item.get('storyteller_uuid', '')
        original_ebook_filename = item['ebook_filename']
        duration = item['duration']
        booklore_id = None
        kosync_doc_id = None

        if storyteller_uuid:
            # Storyteller Tri-Link Logic (mirrors match POST handler)
            try:
                logger.info(f"🔍 Batch Match: Using Storyteller Artifact '{storyteller_uuid}' for '{item['abs_title']}'")

                target_filename, _target_path = _download_storyteller_artifact(
                    storyteller_uuid,
                    item.get('abs_title'),
                    original_ebook_filename=ebook_filename,
                )
                if target_filename:
                    original_ebook_filename = ebook_filename  # Preserve original (may be empty for storyteller-only)
                    ebook_filename = target_filename  # Override filename (artifact or original under NO_EPUB_CACHE)

                    kosync_doc_id = _compute_storyteller_trilink_kosync_id(
                        original_ebook_filename,
                        target_filename,
                        "Batch Match Tri-Link",
                    )
                else:
                    logger.warning(f"⚠️ Failed to obtain Storyteller artifact '{storyteller_uuid}' for '{item['abs_title']}', skipping")
                    continue
            except Exception as e:
                logger.error(f"❌ Storyteller Tri-Link failed for '{item['abs_title']}': {e}", exc_info=True)
                continue
        else:
            # Standard path: Get booklore_id if available for API-based hash computation
            if clients.booklore_client.is_configured():
                book = clients.booklore_client.find_book_by_filename(ebook_filename)
                if book:
                    booklore_id = book.get('id')

            # Compute KOSync ID (Grimmory API first, filesystem fallback)
            _item_ebook_source = (item.get('ebook_source') or '').strip() or None
            kosync_doc_id = get_kosync_id_for_ebook(
                ebook_filename,
                booklore_id,
                bookorbit_id=item.get('ebook_source_id') if _item_ebook_source == 'BookOrbit' else None,
                kavita_id=item.get('ebook_source_id') if _item_ebook_source == 'Kavita' else None,
                source_path=item.get('ebook_source_path'),
            )

        if not kosync_doc_id:
            logger.warning(f"⚠️ Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
            continue

        # Hash Preservation for Batch Match: respect existing hash
        # (including manual overrides) to prevent re-match from reverting.
        current_book_entry = database_service.get_book(item['abs_id'])
        if current_book_entry and current_book_entry.kosync_doc_id:
            logger.info(f"🔄 Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
            kosync_doc_id = current_book_entry.kosync_doc_id

        item_details = clients.abs_client.get_item_details(item['abs_id'])
        chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
        storyteller_manifest = ingest_storyteller_transcripts(
            item['abs_id'],
            item.get('abs_title', ''),
            chapters
        )
        transcript_source = _storyteller_transcript_source(storyteller_uuid, storyteller_manifest)

        # Create Book object and save to database service
        book = Book(
            abs_id=item['abs_id'],
            abs_title=item['abs_title'],
            audio_source="ABS",
            audio_source_id=item['abs_id'],
            audio_title=item['abs_title'],
            audio_cover_url=item.get('cover_url'),
            audio_duration=duration,
            audio_provider_book_id=item['abs_id'],
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=storyteller_manifest,
            status="pending",
            duration=duration,
            transcript_source=transcript_source,
            storyteller_uuid=storyteller_uuid or None,
            original_ebook_filename=original_ebook_filename,
            ebook_source=item.get('ebook_source'),
            ebook_source_id=item.get('ebook_source_id'),
        )

        database_service.save_book(book)
        _claim_book_for_user_id(get_current_user_id(), book.abs_id)

        # Trigger Hardcover/StoryGraph automatch in the background.
        _enqueue_tracker_automatch(clients.sync_clients, book)

        if not str(item['abs_id']).startswith('booklore:'):
            clients.abs_client.add_to_collection(item['abs_id'], user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))
        shelf_filename = original_ebook_filename or ebook_filename
        if not _complete_shelf_watch_approval(shelf_watch_meta) and shelf_filename:
            _shelve_matched_ebook(shelf_filename, item.get('ebook_source'),
                                  item.get('ebook_source_id'))
        if clients.storyteller_client.is_configured():
            if book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

        # Auto-dismiss any pending suggestion
        database_service.dismiss_suggestion(item['abs_id'])
        database_service.dismiss_suggestion(kosync_doc_id)

        # Robust dismissal
        try:
            device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                 database_service.dismiss_suggestion(device_doc.document_hash)
        except Exception: pass


def _process_forge_match_queue(queue_items):
    """Background processor for the batch-match 'Forge & Match' action.

    Like _process_batch_queue but for the forge path; runs off the request thread
    via _spawn_user_background so the page redirects immediately.
    """
    from src.db.models import Book
    clients = uc()
    for item in queue_items:
        shelf_watch_meta = _queue_item_shelf_watch_metadata(item)
        if item.get('audio_only'):
            if _create_audio_only_mapping_from_queue_item(item):
                _complete_shelf_watch_approval(shelf_watch_meta)
            continue
        if not item.get('audio_source'):
            # No audio: ebook-only / storyteller-only item — match only (nothing to forge).
            saved_book = _create_ebook_only_mapping_from_queue_item(item)
            if saved_book and not _complete_shelf_watch_approval(shelf_watch_meta):
                _shelve_saved_ebook(saved_book)
            continue
        audio_source = item.get('audio_source') or 'ABS'
        storyteller_uuid = item.get('storyteller_uuid', '')
        if _normalize_text_source_type(item.get('ebook_source')) == "BookFusion" and item.get('ebook_source_id'):
            saved_book, err_msg, _err_code = _create_or_update_bookfusion_progress_mapping(
                audio_source=audio_source,
                audio_source_id=item.get('audio_source_id') or item.get('abs_id'),
                audio_title=item.get('audio_title') or item.get('abs_title'),
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=_parse_audio_duration(item.get('audio_duration') or item.get('duration')),
                audio_provider_book_id=item.get('audio_provider_book_id'),
                audio_provider_file_id=item.get('audio_provider_file_id'),
                bookfusion_id=item.get('ebook_source_id'),
                bookfusion_title=item.get('ebook_display_name') or item.get('ebook_filename'),
                storyteller_uuid=item.get('storyteller_uuid'),
            )
            if err_msg:
                logger.warning(
                    "Batch Forge skipped BookFusion link for '%s': %s",
                    sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    err_msg,
                )
            elif saved_book:
                _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
                _complete_shelf_watch_approval(shelf_watch_meta)
            continue

        # If Storyteller is selected, keep the current direct-match path.
        if storyteller_uuid:
            if audio_source in _LIBRARY_AUDIO_SOURCES:
                saved_book, err_msg, _err_code = _create_or_update_library_audio_mapping(
                    audio_source=audio_source,
                    audio_source_id=item.get('audio_source_id'),
                    audio_title=item.get('audio_title'),
                    audio_cover_url=item.get('audio_cover_url'),
                    audio_duration=_parse_audio_duration(item.get('audio_duration')),
                    audio_provider_book_id=item.get('audio_provider_book_id'),
                    audio_provider_file_id=item.get('audio_provider_file_id'),
                    ebook_filename=item.get('ebook_filename'),
                    ebook_source=item.get('ebook_source'),
                    ebook_source_id=item.get('ebook_source_id'),
                    storyteller_uuid=storyteller_uuid,
                    ebook_source_path=item.get('ebook_source_path'),
                )
                if err_msg:
                    logger.warning(
                        "Batch Forge skipped %s audiobook '%s': %s",
                        _audio_source_display_name(audio_source),
                        sanitize_log_data(item.get('audio_title') or item.get('audio_source_id')),
                        err_msg,
                    )
                elif saved_book:
                    _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
                    _complete_shelf_watch_approval(shelf_watch_meta, remove_only=True)
                continue

            ebook_filename = item['ebook_filename']
            original_ebook_filename = item['ebook_filename']
            duration = item['duration']
            kosync_doc_id = None

            try:
                logger.info(
                    "Batch Forge: Using Storyteller Artifact '%s' for '%s'",
                    sanitize_log_data(storyteller_uuid),
                    sanitize_log_data(item.get('abs_title')),
                )

                target_filename, _target_path = _download_storyteller_artifact(
                    storyteller_uuid,
                    item.get('abs_title'),
                    original_ebook_filename=ebook_filename,
                )
                if target_filename:
                    original_ebook_filename = ebook_filename
                    ebook_filename = target_filename

                    kosync_doc_id = _compute_storyteller_trilink_kosync_id(
                        original_ebook_filename,
                        target_filename,
                        "Batch Forge Tri-Link",
                    )
                else:
                    logger.warning(
                        "Batch Forge: Failed to obtain Storyteller artifact '%s' for '%s', skipping",
                        sanitize_log_data(storyteller_uuid),
                        sanitize_log_data(item.get('abs_title')),
                    )
                    continue
            except Exception as e:
                logger.error(
                    "Batch Forge: Storyteller Tri-Link failed for '%s': %s",
                    sanitize_log_data(item.get('abs_title')),
                    e,
                    exc_info=True,
                )
                continue

            if not kosync_doc_id:
                logger.warning(
                    "Batch Forge: Could not compute KOSync ID for %s, skipping",
                    sanitize_log_data(ebook_filename),
                )
                continue

            current_book_entry = database_service.get_book(item['abs_id'])
            if current_book_entry and current_book_entry.kosync_doc_id:
                kosync_doc_id = current_book_entry.kosync_doc_id

            item_details = clients.abs_client.get_item_details(item['abs_id'])
            chapters = item_details.get('media', {}).get('chapters', []) if item_details else []
            storyteller_manifest = ingest_storyteller_transcripts(
                item['abs_id'],
                item.get('abs_title', ''),
                chapters
            )
            transcript_source = _storyteller_transcript_source(storyteller_uuid, storyteller_manifest)

            book = Book(
                abs_id=item['abs_id'],
                abs_title=item['abs_title'],
                audio_source="ABS",
                audio_source_id=item['abs_id'],
                audio_title=item['abs_title'],
                audio_cover_url=item.get('cover_url'),
                audio_duration=duration,
                audio_provider_book_id=item['abs_id'],
                ebook_filename=ebook_filename,
                kosync_doc_id=kosync_doc_id,
                transcript_file=storyteller_manifest,
                status="pending",
                duration=duration,
                transcript_source=transcript_source,
                storyteller_uuid=storyteller_uuid or None,
                original_ebook_filename=original_ebook_filename,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
            )

            database_service.save_book(book)
            _claim_book_for_user_id(get_current_user_id(), book.abs_id)

            _enqueue_tracker_automatch(clients.sync_clients, book)

            if not str(item['abs_id']).startswith('booklore:'):
                clients.abs_client.add_to_collection(item['abs_id'], user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))
            shelf_filename = original_ebook_filename or ebook_filename
            if not _complete_shelf_watch_approval(shelf_watch_meta) and shelf_filename:
                _shelve_matched_ebook(
                    shelf_filename, item.get('ebook_source'), item.get('ebook_source_id')
                )
            if clients.storyteller_client.is_configured() and book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

            database_service.dismiss_suggestion(item['abs_id'])
            database_service.dismiss_suggestion(kosync_doc_id)

            try:
                device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
                if device_doc and device_doc.document_hash != kosync_doc_id:
                    database_service.dismiss_suggestion(device_doc.document_hash)
            except Exception:
                pass
            continue

        original_filename = (item.get('ebook_filename') or '').strip()
        if not original_filename:
            logger.warning(
                "Batch Forge skipped '%s': missing ebook filename",
                sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
            )
            continue

        source_type = _normalize_text_source_type(item.get('ebook_source'))
        source_id = str(item.get('ebook_source_id') or '').strip()
        source_path = str(item.get('ebook_source_path') or '').strip()

        if not source_type:
            if source_id:
                source_type = 'Booklore'
            else:
                source_type = 'Local File'
        if source_type == 'Local File' and not source_path:
            resolved_path = find_ebook_file(original_filename)
            source_path = str(resolved_path) if resolved_path else ''

        if source_type in ('ABS', 'Booklore', 'BookOrbit', 'Kavita', 'CWA') and not source_id:
            logger.warning(
                "Batch Forge skipped '%s': missing source id for source type '%s'",
                sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                sanitize_log_data(source_type),
            )
            continue
        if source_type == 'Local File' and not source_path:
            logger.warning(
                "Batch Forge skipped '%s': local file path unavailable",
                sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
            )
            continue

        text_item = _build_forge_text_item(source_type, source_id, source_path, original_filename)
        initial_booklore_id = source_id if text_item.get('source') == 'Booklore' else None
        kosync_doc_id = get_kosync_id_for_ebook(
            original_filename,
            initial_booklore_id,
            bookorbit_id=source_id if text_item.get('source') == 'BookOrbit' else None,
            kavita_id=source_id if text_item.get('source') == 'Kavita' else None,
            source_path=source_path,
        )
        if not kosync_doc_id:
            logger.warning(
                "Batch Forge: Could not compute KOSync ID for '%s', continuing with forge fallback hash",
                sanitize_log_data(original_filename),
            )

        audio_duration = _parse_audio_duration(item.get('audio_duration'))
        if audio_duration is None:
            audio_duration = _parse_audio_duration(item.get('duration'))

        if audio_source in _LIBRARY_AUDIO_SOURCES:
            audio_source_id = (item.get('audio_source_id') or '').strip()
            forge_id = _build_bridge_key(audio_source, audio_source_id)
            if not forge_id:
                logger.warning(
                    "Batch Forge skipped '%s': missing %s source id",
                    sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                    _audio_source_display_name(audio_source),
                )
                continue

            forge_title = item.get('audio_title') or item.get('abs_title') or Path(original_filename).stem
            book = Book(
                abs_id=forge_id,
                abs_title=forge_title,
                ebook_filename=original_filename,
                original_ebook_filename=original_filename,
                kosync_doc_id=kosync_doc_id or f"forging_{forge_id}",
                status="forging",
                duration=audio_duration or 0.0,
                audio_source=audio_source,
                audio_source_id=audio_source_id,
                audio_provider_book_id=item.get('audio_provider_book_id') or audio_source_id,
                audio_provider_file_id=item.get('audio_provider_file_id'),
                audio_title=forge_title,
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=audio_duration,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
            )
            database_service.save_book(book)
            _claim_book_for_user_id(get_current_user_id(), book.abs_id)
            _record_forge_match_job(forge_id, progress=0.02, last_error="Queued Forge & Match")

            container.forge_service().start_auto_forge_match(
                abs_id=forge_id,
                text_item=text_item,
                title=forge_title,
                author=None,
                original_filename=original_filename,
                original_hash=kosync_doc_id,
                audio_source=audio_source,
                audio_source_id=audio_source_id,
                **_client_bundle_kwargs(clients),
            )
        else:
            forge_id = item.get('abs_id')
            if not forge_id:
                logger.warning(
                    "Batch Forge skipped '%s': missing ABS id",
                    sanitize_log_data(item.get('audio_title') or item.get('abs_title')),
                )
                continue

            forge_title = item.get('abs_title') or item.get('audio_title') or forge_id
            book = Book(
                abs_id=forge_id,
                abs_title=forge_title,
                ebook_filename=original_filename,
                original_ebook_filename=original_filename,
                kosync_doc_id=kosync_doc_id or f"forging_{forge_id}",
                status="forging",
                duration=audio_duration or 0.0,
                audio_source='ABS',
                audio_source_id=forge_id,
                audio_provider_book_id=item.get('audio_provider_book_id') or forge_id,
                audio_provider_file_id=item.get('audio_provider_file_id'),
                audio_title=forge_title,
                audio_cover_url=item.get('audio_cover_url') or item.get('cover_url'),
                audio_duration=audio_duration,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
            )
            database_service.save_book(book)
            _claim_book_for_user_id(get_current_user_id(), book.abs_id)
            _record_forge_match_job(forge_id, progress=0.02, last_error="Queued Forge & Match")

            container.forge_service().start_auto_forge_match(
                abs_id=forge_id,
                text_item=text_item,
                title=forge_title,
                author=None,
                original_filename=original_filename,
                original_hash=kosync_doc_id,
                **_client_bundle_kwargs(clients),
            )

        _complete_shelf_watch_approval(shelf_watch_meta)
        database_service.dismiss_suggestion(forge_id)
        if kosync_doc_id:
            database_service.dismiss_suggestion(kosync_doc_id)


def _queue_item_from_match_form(clients) -> "dict | None":
    """Build one canonical match-queue item from the submitted selection."""
    submitted_key = (request.form.get('audiobook_id') or '').strip()
    audio_source = (
        request.form.get('audio_source')
        or _audio_source_from_bridge_key(submitted_key)
    ).strip() or None
    audio_source_id = (
        request.form.get('audio_source_id') or submitted_key or ''
    ).strip() or None
    if audio_source in _LIBRARY_AUDIO_SOURCES and audio_source_id:
        prefix = f"{audio_source.lower()}:"
        if audio_source_id.lower().startswith(prefix):
            audio_source_id = audio_source_id.split(':', 1)[1].strip() or None

    audio_title = (request.form.get('audio_title') or '').strip() or None
    audio_cover_url = (request.form.get('audio_cover_url') or '').strip() or None
    audio_provider_book_id = (
        request.form.get('audio_provider_book_id') or audio_source_id or ''
    ).strip() or None
    audio_provider_file_id = (
        request.form.get('audio_provider_file_id') or ''
    ).strip() or None
    audio_duration = _parse_audio_duration(request.form.get('audio_duration'))
    ebook_filename = request.form.get('ebook_filename', '')
    ebook_display_name = request.form.get('ebook_display_name', ebook_filename)
    ebook_source = (
        request.form.get('ebook_source') or request.form.get('source_type') or ''
    ).strip() or None
    ebook_source_id = (
        request.form.get('ebook_source_id') or request.form.get('source_id') or ''
    ).strip() or None
    ebook_source_path = (
        request.form.get('ebook_source_path') or request.form.get('source_path') or ''
    ).strip() or None
    storyteller_uuid = request.form.get('storyteller_uuid', '') or ''
    audio_only = (request.form.get('audio_only') or '').strip().lower() in {
        'true', '1', 'yes', 'on'
    }

    bridge_key = None
    if audio_source == 'ABS' and audio_source_id:
        selected_ab = None
        if not audio_title or audio_duration is None:
            audiobooks = get_audiobooks_conditionally() or []
            selected_ab = next(
                (
                    audiobook for audiobook in audiobooks
                    if str(audiobook.get('id')) == audio_source_id
                ),
                None,
            )
        bridge_key = _build_bridge_key(audio_source, audio_source_id)
        audio_title = audio_title or (
            manager.get_abs_title(selected_ab) if selected_ab else audio_source_id
        )
        if audio_duration is None and selected_ab:
            audio_duration = manager.get_duration(selected_ab)
        audio_cover_url = audio_cover_url or f"/api/cover-proxy/{audio_source_id}"
    elif audio_source in _LIBRARY_AUDIO_SOURCES and audio_source_id:
        bridge_key = _build_bridge_key(audio_source, audio_source_id)
        audio_title = audio_title or (
            f"{_audio_source_display_name(audio_source)} {audio_source_id}"
        )
    elif not audio_source and (ebook_filename or storyteller_uuid):
        ebook_key = (storyteller_uuid or ebook_filename).strip()
        if ebook_key:
            bridge_key = f"ebook:{ebook_key}"
            audio_source_id = None
            audio_title = ebook_display_name or (
                Path(ebook_filename).stem if ebook_filename else 'Ebook'
            )
            audio_duration = None
            audio_cover_url = None
            audio_provider_book_id = None
            audio_provider_file_id = None

    if not bridge_key or not (ebook_filename or storyteller_uuid or audio_only):
        return None
    # A submitted form field can still carry a legacy absolute cover URL.
    safe_cover_url = _browser_cover_url(
        audio_cover_url,
        audio_source=audio_source,
        audio_source_id=audio_source_id,
        abs_id=bridge_key if audio_source else None,
    ) or None
    return {
        'bridge_key': bridge_key,
        'abs_id': bridge_key,
        'audio_source': audio_source,
        'audio_source_id': audio_source_id,
        'audio_title': audio_title,
        'abs_title': audio_title,
        'audio_duration': audio_duration,
        'duration': audio_duration,
        'audio_cover_url': safe_cover_url,
        'cover_url': safe_cover_url,
        'audio_provider_book_id': audio_provider_book_id,
        'audio_provider_file_id': audio_provider_file_id,
        'ebook_filename': ebook_filename,
        'ebook_display_name': ebook_display_name,
        'ebook_source': ebook_source,
        'ebook_source_id': ebook_source_id,
        'ebook_source_path': ebook_source_path,
        'storyteller_uuid': storyteller_uuid,
        'audio_only': audio_only and not (ebook_filename or storyteller_uuid),
    }



def _add_book_view():
    """Render and handle the unified queue-based Add Book view."""
    clients = uc()
    storyteller_enabled = bool(clients.storyteller_client.is_configured())
    if request.method == 'POST':
        action = request.form.get('action')
        if action in ('forge_and_match_queue', 'forge_only_queue') and not storyteller_enabled:
            flash("Configure Storyteller before creating a Storyteller edition.", "warning")
            return redirect(url_for('add_book'))
        if action == 'add_to_queue':
            queue_item = _queue_item_from_match_form(clients)
            if queue_item:
                _match_queue_add(queue_item)
            # Clear the search box + results after queueing so the user starts the
            # next book from a clean search (drop the preserved `search` term).
            return redirect(url_for('add_book'))
        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            _match_queue_remove(abs_id)
            return redirect(url_for('add_book'))
        elif action == 'clear_queue':
            _match_queue_clear()
            return redirect(url_for('add_book'))
        elif action == 'forge_and_match_queue':
            _queue_items = _match_queue_drain()
            _spawn_user_background(_process_forge_match_queue, _queue_items, label="batch-forge-match")
            flash(f"Forging + matching {len(_queue_items)} book(s) in the background…", "info")
            return redirect(url_for('index'))
        elif action == 'forge_only_queue':
            _queue_items = _match_queue_drain()
            forge_stage_mode = (request.form.get('forge_stage_mode') or '').strip() or None
            _spawn_user_background(_process_forge_only_queue, _queue_items, forge_stage_mode, label="add-book-forge-only")
            flash(f"Forging {len(_queue_items)} edition(s) in the background…", "info")
            return redirect(url_for('index'))
        elif action == 'process_queue':
            _queue_items = _match_queue_drain()
            _spawn_user_background(_process_batch_queue, _queue_items, label="batch-match-process")
            flash(f"Processing {len(_queue_items)} book(s) in the background…", "info")
            return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        audiobooks = _search_audiobooks_with_fallback(search)

        # Use new search method
        ebooks = _search_ebooks_with_fallback(search)
        ebooks.sort(key=lambda x: x.name.lower())
        ebooks = _promote_authoritative_ebook_matches(audiobooks, ebooks)

        # Search Storyteller
        if storyteller_enabled:
            try:
                storyteller_books = clients.storyteller_client.search_books(search)
            except Exception as e:
                logger.warning(f"⚠️ Storyteller search failed in add_book route: {e}", exc_info=True)

    return render_template('add_book.html', audiobooks=audiobooks, ebooks=ebooks,
                           storyteller_books=storyteller_books,
                           queue=_load_match_queue(), search=search,
                           storyteller_enabled=storyteller_enabled)


def batch_match():
    """Preserve legacy `/batch-match` requests through the unified Add Book flow."""
    if request.method == 'GET':
        search = request.args.get('search', '')
        return redirect(url_for('add_book', search=search), code=302)
    return _add_book_view()


def add_book():
    """Unified `/add-book` page — single, batch, forge, and match in one queue-based flow."""
    return _add_book_view()


def _get_suggestions_service():
    from src.services.suggestions_service import SuggestionsService

    try:
        calibre_resolver = container.calibre_identifier_resolver()
    except Exception:
        calibre_resolver = None

    try:
        ollama_client = container.ollama_client()
    except Exception:
        ollama_client = None

    return SuggestionsService(
        database_service=database_service,
        container=container,
        manager=manager,
        get_audiobooks_conditionally=get_suggestion_audiobooks,
        get_searchable_ebooks=get_searchable_ebooks,
        audiobook_matches_search=audiobook_matches_search,
        get_abs_author=get_abs_author,
        logger=logger,
        calibre_identifier_resolver=calibre_resolver,
        ollama_client=ollama_client,
    )


def _get_ignored_suggestion_source_ids():
    """Return suggestion source IDs (bridge keys) that are marked as ignored."""
    return _get_suggestions_service().get_ignored_suggestion_source_ids()


def scan_library_suggestions(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None, progress_callback=None):
    """Scan for unmatched audiobooks and find candidate ebook matches."""
    return _get_suggestions_service().scan_library_suggestions(
        cached_suggestions_by_abs=cached_suggestions_by_abs,
        cached_no_match_abs_ids=cached_no_match_abs_ids,
        progress_callback=progress_callback,
    )


def _prune_suggestions_scan_jobs():
    cutoff = time.time() - SUGGESTIONS_SCAN_JOB_TTL_SECONDS
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        stale_ids = [
            job_id for job_id, job in SUGGESTIONS_SCAN_JOBS.items()
            if job.get('updated_at', job.get('started_at', 0)) < cutoff
        ]
        for job_id in stale_ids:
            SUGGESTIONS_SCAN_JOBS.pop(job_id, None)


def _start_suggestions_scan_job(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None):
    _prune_suggestions_scan_jobs()
    job_id = uuid.uuid4().hex
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        SUGGESTIONS_SCAN_JOBS[job_id] = {
            "status": "running",
            "results": {},
            "error": None,
            "progress": {
                "phase": "initializing",
                "percent": 0,
                "message": "Preparing scan...",
                "scanned_new_done": 0,
                "scanned_new_total": 0,
                "reused_cached": 0,
                "total_unmatched": 0,
            },
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    bundle = uc()
    try:
        user = current_user()
        user_id = user.id if user is not None else None
    except Exception:
        user_id = None
    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            user_id = None
    creds = get_current_user_credentials()

    def runner():
        tok_bundle = _active_bundle.set(bundle)
        tok_uid = set_current_user_id(user_id)
        tok_creds = set_current_user_credentials(creds)
        try:
            _run_suggestions_scan_job(
                job_id,
                cached_suggestions_by_abs or {},
                cached_no_match_abs_ids or [],
            )
        finally:
            reset_current_user_credentials(tok_creds)
            reset_current_user_id(tok_uid)
            _active_bundle.reset(tok_bundle)

    if _BACKGROUND_TASKS_SYNCHRONOUS:
        runner()
    else:
        threading.Thread(target=runner, daemon=True, name="suggestions-scan").start()
    return job_id


def _auto_match_threshold() -> float:
    """Return the auto-match threshold as a float (0-100 scale).

    Reads the SUGGESTIONS_AUTO_MATCH_THRESHOLD setting from the environment,
    clamping to the valid range. Defaults to 100.0.
    """
    try:
        value = float(os.environ.get('SUGGESTIONS_AUTO_MATCH_THRESHOLD', '100'))
    except (TypeError, ValueError):
        value = 100.0
    return max(0.0, min(value, 100.0))


def _is_same_folder_match(match: dict) -> bool:
    """Return True if the match is a same-folder candidate and should be excluded from auto-matching.

    Same-folder matches (match_reason starting with 'same_folder') receive a score of 100.0
    or 94.0 merely because the audiobook and ebook share a folder with a loose title
    agreement (fuzzy ratio >= 45). They are explicitly surfaced for human review with a
    'Same folder?' badge and must not be auto-linked, as a wrong link would sync a
    reader's position into the wrong book.
    """
    reason = match.get('match_reason') or ''
    return reason.startswith('same_folder')


def _auto_match_suggestions(results: object, user_id: int | None) -> object:
    """Auto-link suggestions whose best eligible candidate meets the threshold.

    Walks the suggestions produced by a library scan, selects the highest-scoring
    *eligible* candidate from each suggestion's matches list (excluding same-folder
    matches), and if that candidate's score is at or above the configurable threshold,
    links the book automatically via book_mapping_service.create_audio_mapping_from_match.
    Suggestions that are not auto-matched remain in the list for human review.

    Args:
        results: The scan results object (expected to be a dict with 'suggestions' key).
        user_id: Ambient user id to own the created mappings, or None.

    Returns:
        The (potentially modified) results object.
    """
    if not env_truthy('SUGGESTIONS_AUTO_MATCH_ENABLED'):
        return results
    if not isinstance(results, dict):
        return results
    suggestions = results.get('suggestions') or []
    if not suggestions:
        return results

    threshold = _auto_match_threshold()
    mapping_service = container.book_mapping_service()
    cache_by_abs = results.get('cache_by_abs') or {}
    remaining = []
    matched = 0

    for suggestion in suggestions:
        matches = suggestion.get('matches') or []
        eligible_matches = [m for m in matches if not _is_same_folder_match(m)]
        best_overall = max(matches, key=lambda m: m.get('score') or 0.0) if matches else None
        top = max(eligible_matches, key=lambda m: m.get('score') or 0.0) if eligible_matches else None
        if best_overall is not None and _is_same_folder_match(best_overall):
            # The highest-scoring candidate was suppressed. Log the decision and its
            # reason so an operator can see why a 100-scoring match was not linked.
            logger.debug(
                f"Auto-match excluded the top same-folder candidate for "
                f"'{suggestion.get('abs_title')}' "
                f"(score={best_overall.get('score') or 0.0:.1f}) — left for review"
            )
        score = float(top.get('score') or 0.0) if top else 0.0
        if not top or score < threshold:
            remaining.append(suggestion)
            continue
        try:
            saved = mapping_service.create_audio_mapping_from_match(
                audio_source=suggestion.get('audio_source') or 'ABS',
                audio_source_id=suggestion.get('audio_source_id') or suggestion.get('abs_id') or '',
                audio_title=suggestion.get('audio_title') or suggestion.get('abs_title') or '',
                ebook_filename=top.get('ebook_filename') or '',
                audio_cover_url=suggestion.get('audio_cover_url'),
                audio_duration=suggestion.get('audio_duration') or suggestion.get('duration'),
                audio_provider_book_id=suggestion.get('audio_provider_book_id'),
                audio_provider_file_id=suggestion.get('audio_provider_file_id'),
                ebook_source=top.get('source'),
                ebook_source_id=str(top.get('source_id')) if top.get('source_id') is not None else None,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"⚠️ Auto-match failed for '{suggestion.get('abs_title')}': {e}", exc_info=True)
            remaining.append(suggestion)
            continue
        if not saved:
            remaining.append(suggestion)
            continue
        matched += 1
        cache_by_abs.pop(suggestion.get('abs_id'), None)
        logger.info(
            f"🔗 Auto-matched '{suggestion.get('abs_title')}' to "
            f"'{top.get('display_name') or top.get('ebook_filename')}' at {score:.1f}%"
        )

    if matched:
        results['suggestions'] = remaining
        results['cache_by_abs'] = cache_by_abs
        stats = results.get('stats') or {}
        stats['auto_matched'] = matched
        results['stats'] = stats
        logger.info(f"🔗 Auto-match created {matched} mapping(s) at or above {threshold:.1f}%")
    return results


def _run_suggestions_scan_job(job_id, cached_suggestions_by_abs=None, cached_no_match_abs_ids=None):
    def update_progress(progress_payload):
        with SUGGESTIONS_SCAN_JOBS_LOCK:
            if job_id in SUGGESTIONS_SCAN_JOBS:
                SUGGESTIONS_SCAN_JOBS[job_id]["progress"] = progress_payload or {}
                SUGGESTIONS_SCAN_JOBS[job_id]["updated_at"] = time.time()

    try:
        results = scan_library_suggestions(
            cached_suggestions_by_abs=cached_suggestions_by_abs,
            cached_no_match_abs_ids=cached_no_match_abs_ids,
            progress_callback=update_progress,
        )
        results = _auto_match_suggestions(results, get_current_user_id())
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": results.get('cache_by_abs', {}) if isinstance(results, dict) else {},
            "scan_cache_no_match_abs_ids": results.get('no_match_abs_ids', []) if isinstance(results, dict) else [],
            "scan_last_stats": results.get('stats', {}) if isinstance(results, dict) else {},
        })
        status = "done"
        error = None
    except Exception as e:
        logger.exception(f"Suggestions scan job failed ({job_id}): {e}")
        results = {}
        status = "error"
        error = str(e)
        update_progress({
            "phase": "error",
            "percent": 100,
            "message": "Scan failed",
            "scanned_new_done": 0,
            "scanned_new_total": 0,
            "reused_cached": 0,
            "total_unmatched": 0,
        })

    with SUGGESTIONS_SCAN_JOBS_LOCK:
        if job_id in SUGGESTIONS_SCAN_JOBS:
            SUGGESTIONS_SCAN_JOBS[job_id].update({
                "status": status,
                "results": results,
                "error": error,
                "updated_at": time.time(),
            })


def _get_suggestions_scan_job(job_id):
    if not job_id:
        return None
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        job = SUGGESTIONS_SCAN_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _clear_legacy_suggestions_session_payload():
    """Remove old large suggestions payload keys from cookie-backed session."""
    legacy_keys = (
        'scan_results',
        'scan_cache_by_abs',
        'scan_cache_no_match_abs_ids',
        'scan_last_stats',
    )
    removed = False
    for key in legacy_keys:
        if key in session:
            session.pop(key, None)
            removed = True
    if removed:
        session.modified = True


def _prune_suggestions_state_store():
    cutoff = time.time() - SUGGESTIONS_STATE_TTL_SECONDS
    with SUGGESTIONS_STATE_LOCK:
        stale_ids = [
            state_id for state_id, state in SUGGESTIONS_STATE_STORE.items()
            if state.get('updated_at', state.get('created_at', 0)) < cutoff
        ]
        for state_id in stale_ids:
            SUGGESTIONS_STATE_STORE.pop(state_id, None)


def _default_suggestions_state():
    now = time.time()
    return {
        "scan_results": [],
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "scan_has_run": False,
        "created_at": now,
        "updated_at": now,
    }


def _get_suggestions_state(create=True):
    _prune_suggestions_state_store()
    state_id = session.get('suggestions_state_id')
    if not state_id and create:
        state_id = uuid.uuid4().hex
        session['suggestions_state_id'] = state_id
        session.modified = True

    if not state_id:
        return None, None

    with SUGGESTIONS_STATE_LOCK:
        state = SUGGESTIONS_STATE_STORE.get(state_id)
        if not state and create:
            state = _default_suggestions_state()
            SUGGESTIONS_STATE_STORE[state_id] = state

        if state:
            state['updated_at'] = time.time()
        return state_id, state


def _suggestions_cache_scope_key(user_id=None):
    if user_id is None:
        try:
            user_id = get_current_user_id()
        except Exception:
            user_id = None
    if user_id is None:
        return "global"
    return f"user_{re.sub(r'[^A-Za-z0-9_.-]+', '_', str(user_id))}"


def _suggestions_cache_file_path(user_id=None):
    scope = _suggestions_cache_scope_key(user_id)
    if scope == "global":
        return DATA_DIR / SUGGESTIONS_CACHE_FILE_NAME
    return DATA_DIR / f"suggestions_scan_cache_{scope}.json"


def _empty_suggestions_cache_payload():
    return {
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "updated_at": time.time(),
    }


def _load_persisted_suggestions_cache():
    cache_file = _suggestions_cache_file_path()
    if not cache_file.exists():
        return _empty_suggestions_cache_payload()

    with SUGGESTIONS_CACHE_LOCK:
        try:
            raw = json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning(f"Could not read suggestions cache file '{cache_file}': {e}", exc_info=True)
            return _empty_suggestions_cache_payload()

    payload = _empty_suggestions_cache_payload()
    if isinstance(raw, dict):
        cache_by_abs = raw.get('scan_cache_by_abs', {})
        no_match = raw.get('scan_cache_no_match_abs_ids', [])
        stats = raw.get('scan_last_stats', {})

        payload['scan_cache_by_abs'] = cache_by_abs if isinstance(cache_by_abs, dict) else {}
        payload['scan_cache_no_match_abs_ids'] = no_match if isinstance(no_match, list) else []
        payload['scan_last_stats'] = stats if isinstance(stats, dict) else {}

    return payload


def _save_persisted_suggestions_cache(payload):
    cache_file = _suggestions_cache_file_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    safe_payload = {
        "scan_cache_by_abs": payload.get('scan_cache_by_abs', {}) if isinstance(payload.get('scan_cache_by_abs', {}), dict) else {},
        "scan_cache_no_match_abs_ids": payload.get('scan_cache_no_match_abs_ids', []) if isinstance(payload.get('scan_cache_no_match_abs_ids', []), list) else [],
        "scan_last_stats": payload.get('scan_last_stats', {}) if isinstance(payload.get('scan_last_stats', {}), dict) else {},
        "updated_at": time.time(),
    }

    temp_file = cache_file.with_suffix('.tmp')
    with SUGGESTIONS_CACHE_LOCK:
        try:
            temp_file.write_text(json.dumps(safe_payload, ensure_ascii=False), encoding='utf-8')
            temp_file.replace(cache_file)
        except Exception as e:
            logger.warning(f"Could not persist suggestions cache file '{cache_file}': {e}", exc_info=True)
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass


def _rehydrate_suggestions_state_from_cache(suggestions_state: dict) -> dict:
    """Rehydrate in-memory suggestions state from the persisted per-user cache.

    If the state already has scan results (non-empty scan_cache_by_abs), the
    in-memory state wins and the cache is not applied. Otherwise, loads the
    persisted cache and populates the state with its contents, including
    rebuilding scan_results sorted by top match score descending.
    """
    if not suggestions_state:
        return suggestions_state

    if suggestions_state.get('scan_cache_by_abs'):
        return suggestions_state

    persisted = _load_persisted_suggestions_cache()
    cache_by_abs = persisted.get('scan_cache_by_abs', {}) or {}
    if not cache_by_abs:
        return suggestions_state

    suggestions_state['scan_cache_by_abs'] = cache_by_abs
    suggestions_state['scan_cache_no_match_abs_ids'] = persisted.get('scan_cache_no_match_abs_ids', []) or []
    suggestions_state['scan_last_stats'] = persisted.get('scan_last_stats', {}) or {}
    suggestions_state['scan_has_run'] = True
    suggestions_state['updated_at'] = time.time()

    suggestions_list = list(cache_by_abs.values())
    suggestions_list.sort(
        key=lambda s: (s.get('matches', [{}])[0].get('score', 0) if s.get('matches') else 0),
        reverse=True,
    )
    suggestions_state['scan_results'] = suggestions_list

    logger.info(f"♻️ Restored {len(suggestions_list)} cached suggestion(s) from the persisted scan cache")
    return suggestions_state


def _match_queue_file_path():
    return DATA_DIR / MATCH_QUEUE_FILE_NAME


def _read_match_queue_unlocked() -> list:
    queue_file = _match_queue_file_path()
    if not queue_file.exists():
        return []
    try:
        raw = json.loads(queue_file.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f"Could not read match queue file '{queue_file}': {e}", exc_info=True)
        return []
    items = raw.get('items', []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    valid_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        owner_id = item.get('user_id')
        if owner_id is not None and _normalize_match_queue_user_id(owner_id) is None:
            logger.warning("Discarding match queue item with malformed owner id")
            continue
        valid_items.append(item)
    return valid_items


def _write_match_queue_unlocked(items: list) -> None:
    queue_file = _match_queue_file_path()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": items if isinstance(items, list) else [], "updated_at": time.time()}
    temp_file = queue_file.with_suffix('.tmp')
    try:
        temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temp_file.replace(queue_file)
    except Exception as e:
        logger.warning(f"Could not persist match queue file '{queue_file}': {e}", exc_info=True)
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass


def _normalize_match_queue_user_id(value):
    max_user_id = (1 << 63) - 1
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 0 < value <= max_user_id else None
    if isinstance(value, str):
        stripped = value.strip()
        if (
            not stripped
            or len(stripped) > 19
            or not stripped.isascii()
            or not stripped.isdecimal()
        ):
            return None
        try:
            user_id = int(stripped)
        except (TypeError, ValueError):
            return None
        return user_id if 0 < user_id <= max_user_id else None
    return None


def _match_queue_scope() -> tuple:
    """Return the acting user id and whether legacy unowned items are visible."""
    user_id = get_current_user_id()
    if user_id is None:
        return None, True
    try:
        primary_user_id = database_service._default_user_id()
    except Exception as exc:
        logger.warning("Could not resolve legacy match-queue owner: %s", exc, exc_info=True)
        return user_id, False
    normalized_user_id = _normalize_match_queue_user_id(user_id)
    normalized_primary_id = _normalize_match_queue_user_id(primary_user_id)
    return user_id, (
        normalized_user_id is not None
        and normalized_user_id == normalized_primary_id
    )


def _match_queue_item_visible(item: dict, user_id, include_legacy: bool) -> bool:
    owner_id = item.get('user_id')
    if owner_id is None:
        return include_legacy
    if user_id is None:
        return False
    normalized_owner_id = _normalize_match_queue_user_id(owner_id)
    normalized_user_id = _normalize_match_queue_user_id(user_id)
    return (
        normalized_owner_id is not None
        and normalized_owner_id == normalized_user_id
    )


def _match_queue_stamp(item: dict, user_id) -> dict:
    scoped_item = dict(item)
    scoped_item['user_id'] = user_id
    return scoped_item


def _clear_match_queue_for_user_id(user_id) -> None:
    """Remove persisted queue items belonging to a successfully deleted user."""
    normalized_user_id = _normalize_match_queue_user_id(user_id)
    if normalized_user_id is None:
        return
    with MATCH_QUEUE_LOCK:
        _write_match_queue_unlocked([
            item for item in _read_match_queue_unlocked()
            if _normalize_match_queue_user_id(item.get('user_id')) != normalized_user_id
        ])


def _load_match_queue() -> list:
    """Return only the acting user's persisted batch-match queue items."""
    user_id, include_legacy = _match_queue_scope()
    with MATCH_QUEUE_LOCK:
        return [
            item for item in _read_match_queue_unlocked()
            if _match_queue_item_visible(item, user_id, include_legacy)
        ]


def _save_match_queue(items: list) -> None:
    """Replace the acting user's queue while preserving every other user's items."""
    user_id, include_legacy = _match_queue_scope()
    replacements = [
        _match_queue_stamp(item, user_id)
        for item in items if isinstance(item, dict)
    ]
    with MATCH_QUEUE_LOCK:
        preserved = [
            item for item in _read_match_queue_unlocked()
            if not _match_queue_item_visible(item, user_id, include_legacy)
        ]
        _write_match_queue_unlocked(preserved + replacements)


def _match_queue_add(item: dict) -> bool:
    """Append an actor-owned item unless its bridge key is already in that scope."""
    user_id, include_legacy = _match_queue_scope()
    scoped_item = _match_queue_stamp(item, user_id)
    with MATCH_QUEUE_LOCK:
        items = _read_match_queue_unlocked()
        bridge_key = scoped_item.get('bridge_key')
        if bridge_key and any(
            existing.get('bridge_key') == bridge_key
            and _match_queue_item_visible(existing, user_id, include_legacy)
            for existing in items
        ):
            return False
        items.append(scoped_item)
        _write_match_queue_unlocked(items)
        return True


def _match_queue_remove(abs_id) -> None:
    user_id, include_legacy = _match_queue_scope()
    with MATCH_QUEUE_LOCK:
        items = [
            item for item in _read_match_queue_unlocked()
            if not (
                item.get('abs_id') == abs_id
                and _match_queue_item_visible(item, user_id, include_legacy)
            )
        ]
        _write_match_queue_unlocked(items)


def _match_queue_clear() -> None:
    user_id, include_legacy = _match_queue_scope()
    with MATCH_QUEUE_LOCK:
        preserved = [
            item for item in _read_match_queue_unlocked()
            if not _match_queue_item_visible(item, user_id, include_legacy)
        ]
        _write_match_queue_unlocked(preserved)


def _match_queue_drain() -> list:
    """Atomically drain only the acting user's queued items."""
    user_id, include_legacy = _match_queue_scope()
    with MATCH_QUEUE_LOCK:
        drained = []
        preserved = []
        for item in _read_match_queue_unlocked():
            if _match_queue_item_visible(item, user_id, include_legacy):
                drained.append(item)
            else:
                preserved.append(item)
        _write_match_queue_unlocked(preserved)
        return drained


def _match_queue_response():
    """Re-render the queue panel fragment for an XHR add/remove/clear (so the page
    isn't reloaded and scroll position is preserved); otherwise redirect to /suggestions."""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template(
            '_match_queue_panel.html',
            queue=_load_match_queue(),
            storyteller_enabled=bool(uc().storyteller_client.is_configured()),
        )
    return redirect(url_for('suggestions'))


def _queue_item_from_suggestion(suggestion: dict) -> "dict | None":
    """Build a batch-match queue item from a scanned suggestion's top match (the ebook
    BookBridge is most confident about). Used by bulk add ('add all exact' / 'add
    selected'); mirrors the item shape produced by the single add_to_queue handler.
    Storyteller tri-links stay a manual per-item choice, so storyteller_uuid is left blank."""
    matches = (suggestion or {}).get('matches') or []
    bridge_key = (suggestion or {}).get('bridge_key') or (suggestion or {}).get('abs_id')
    if not matches or not bridge_key:
        return None
    top = matches[0]
    ebook_filename = top.get('ebook_filename') or ''
    if not ebook_filename:
        return None
    audio_title = suggestion.get('audio_title') or suggestion.get('abs_title') or ''
    audio_duration = suggestion.get('audio_duration')
    if audio_duration is None:
        audio_duration = suggestion.get('duration')
    audio_cover_url = suggestion.get('audio_cover_url') or suggestion.get('cover_url')
    return {
        'bridge_key': bridge_key,
        'abs_id': bridge_key,
        'audio_source': suggestion.get('audio_source'),
        'audio_source_id': suggestion.get('audio_source_id'),
        'audio_title': audio_title,
        'abs_title': audio_title,
        'audio_duration': audio_duration,
        'duration': audio_duration,
        'audio_cover_url': audio_cover_url,
        'cover_url': audio_cover_url,
        'audio_provider_book_id': suggestion.get('audio_provider_book_id'),
        'audio_provider_file_id': suggestion.get('audio_provider_file_id'),
        'ebook_filename': ebook_filename,
        'ebook_display_name': top.get('display_name') or ebook_filename,
        'ebook_source': top.get('source'),
        'ebook_source_id': top.get('source_id'),
        'ebook_source_path': top.get('source_path') or '',
        'storyteller_uuid': '',
    }


def suggestions_page():
    _clear_legacy_suggestions_session_payload()
    clients = uc()
    storyteller_enabled = bool(clients.storyteller_client.is_configured())
    state_id, suggestions_state = _get_suggestions_state(create=True)
    if suggestions_state is None:
        suggestions_state = _default_suggestions_state()

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'forge_and_match_queue' and not storyteller_enabled:
            flash("Configure Storyteller before creating a Storyteller edition.", "warning")
            return redirect(url_for('suggestions'))

        if action in ('scan', 'scan_full'):
            full_refresh = (action == 'scan_full')
            if full_refresh:
                cached_suggestions_by_abs = {}
                cached_no_match_abs_ids = []
                suggestions_state['scan_cache_by_abs'] = {}
                suggestions_state['scan_cache_no_match_abs_ids'] = []
                suggestions_state['scan_last_stats'] = {}
                suggestions_state['scan_results'] = []
                suggestions_state['scan_has_run'] = False
                suggestions_state['updated_at'] = time.time()
                _save_persisted_suggestions_cache(_empty_suggestions_cache_payload())
            else:
                state_cache = suggestions_state.get('scan_cache_by_abs', {}) or {}
                state_no_match = suggestions_state.get('scan_cache_no_match_abs_ids', []) or []
                if state_cache or state_no_match:
                    cached_suggestions_by_abs = state_cache
                    cached_no_match_abs_ids = state_no_match
                else:
                    persisted_cache = _load_persisted_suggestions_cache()
                    cached_suggestions_by_abs = persisted_cache.get('scan_cache_by_abs', {}) or {}
                    cached_no_match_abs_ids = persisted_cache.get('scan_cache_no_match_abs_ids', []) or []

            job_id = _start_suggestions_scan_job(
                cached_suggestions_by_abs=cached_suggestions_by_abs,
                cached_no_match_abs_ids=cached_no_match_abs_ids,
            )
            session['suggestions_scan_job_id'] = job_id
            session.modified = True

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True, "status": "running", "job_id": job_id, "full_refresh": full_refresh})
            return redirect(url_for('suggestions'))

        elif action == 'never':
            bridge_key = (request.form.get('bridge_key') or request.form.get('abs_id') or '').strip()
            if bridge_key:
                from src.db.models import PendingSuggestion

                current_scan_results = suggestions_state.get('scan_results', [])
                current_entry = next(
                    (
                        s for s in current_scan_results
                        if (s.get('bridge_key') or s.get('abs_id')) == bridge_key
                    ),
                    None,
                )
                audio_source = (
                    request.form.get('audio_source')
                    or (current_entry.get('audio_source') if current_entry else '')
                    or _audio_source_from_bridge_key(bridge_key)
                ).strip() or 'ABS'
                abs_title = (
                    request.form.get('audio_title')
                    or request.form.get('abs_title')
                    or (current_entry.get('audio_title') if current_entry else '')
                    or (current_entry.get('abs_title') if current_entry else '')
                    or ''
                )
                abs_author = (
                    request.form.get('audio_author')
                    or request.form.get('abs_author')
                    or (current_entry.get('audio_author') if current_entry else '')
                    or (current_entry.get('abs_author') if current_entry else '')
                    or ''
                )
                cover_url = request.form.get('cover_url') or (current_entry.get('cover_url') if current_entry else '') or ''

                if not database_service.ignore_suggestion(bridge_key):
                    suggestion = PendingSuggestion(
                        source_id=bridge_key,
                        title=abs_title,
                        author=abs_author,
                        cover_url=cover_url,
                        matches_json="[]",
                        status='ignored'
                    )
                    suggestion.source = audio_source
                    database_service.save_pending_suggestion(suggestion)

                suggestions_state['scan_results'] = [
                    item
                    for item in current_scan_results
                    if (item.get('bridge_key') or item.get('abs_id')) != bridge_key
                ]
                cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
                if bridge_key in cache_by_abs:
                    cache_by_abs.pop(bridge_key, None)
                    suggestions_state['scan_cache_by_abs'] = cache_by_abs
                no_match_abs_ids = [
                    x for x in (suggestions_state.get('scan_cache_no_match_abs_ids', []) or [])
                    if x != bridge_key
                ]
                suggestions_state['scan_cache_no_match_abs_ids'] = no_match_abs_ids
                suggestions_state['updated_at'] = time.time()

            return redirect(url_for('suggestions'))

        elif action == 'add_to_queue':
            queue_item = _queue_item_from_match_form(clients)
            if queue_item:
                _match_queue_add(queue_item)
            return _match_queue_response()

        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            _match_queue_remove(abs_id)
            return _match_queue_response()

        elif action == 'clear_queue':
            _match_queue_clear()
            return _match_queue_response()

        elif action == 'add_many_to_queue':
            keys = request.form.getlist('bridge_keys')
            suggestions_state = _rehydrate_suggestions_state_from_cache(suggestions_state)
            cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}

            requested = len(keys)
            added = 0
            skipped_missing = 0
            skipped_no_item = 0
            already_queued = 0

            for key in keys:
                suggestion = cache_by_abs.get(key)
                if not suggestion:
                    skipped_missing += 1
                    continue
                item = _queue_item_from_suggestion(suggestion)
                if not item:
                    skipped_no_item += 1
                    continue
                if _match_queue_add(item):
                    added += 1
                else:
                    already_queued += 1

            if requested > 0 and added == 0 and skipped_missing == requested:
                logger.warning(
                    f"⚠️ add_many_to_queue: {requested} key(s) requested but scan cache had no entry for any of them"
                )
            else:
                logger.info(
                    f"✅ add_many_to_queue: requested={requested} added={added} skipped_missing={skipped_missing} skipped_no_item={skipped_no_item} already_queued={already_queued}"
                )
            return _match_queue_response()

        elif action == 'forge_and_match_queue':
            # Same forge/match-all path as the Add Book page: Storyteller items just match,
            # ebook-only items run the forge (transcribe + align) pipeline in the background.
            _queue_items = _match_queue_drain()
            _spawn_user_background(_process_forge_match_queue, _queue_items, label="batch-forge-match")
            flash(f"Forging + matching {len(_queue_items)} book(s) in the background…", "info")
            return redirect(url_for('index'))

        elif action == 'process_queue':
            _queue_items = _match_queue_drain()
            _spawn_user_background(_process_batch_queue, _queue_items, label="batch-match-process")
            flash(f"Processing {len(_queue_items)} book(s) in the background…", "info")
            return redirect(url_for('index'))

    scan_in_progress = False
    scan_error = None
    job_id = session.get('suggestions_scan_job_id')
    if job_id:
        scan_job = _get_suggestions_scan_job(job_id)
        if not scan_job:
            session.pop('suggestions_scan_job_id', None)
            session.modified = True
        else:
            status = scan_job.get('status')
            if status == 'done':
                scan_payload = scan_job.get('results', {}) or {}
                suggestions_state['scan_results'] = scan_payload.get('suggestions', [])
                suggestions_state['scan_cache_by_abs'] = scan_payload.get('cache_by_abs', {})
                suggestions_state['scan_cache_no_match_abs_ids'] = scan_payload.get('no_match_abs_ids', [])
                suggestions_state['scan_last_stats'] = scan_payload.get('stats', {})
                suggestions_state['scan_has_run'] = True
                suggestions_state['updated_at'] = time.time()
                _save_persisted_suggestions_cache({
                    "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
                    "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
                    "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
                })
                session.pop('suggestions_scan_job_id', None)
                session.modified = True
                with SUGGESTIONS_SCAN_JOBS_LOCK:
                    SUGGESTIONS_SCAN_JOBS.pop(job_id, None)
            elif status == 'error':
                scan_error = scan_job.get('error') or 'Scan failed'
                session.pop('suggestions_scan_job_id', None)
                session.modified = True
                with SUGGESTIONS_SCAN_JOBS_LOCK:
                    SUGGESTIONS_SCAN_JOBS.pop(job_id, None)
            else:
                scan_in_progress = True

    if not scan_in_progress:
        suggestions_state = _rehydrate_suggestions_state_from_cache(suggestions_state)

    ignored_source_ids = _get_ignored_suggestion_source_ids()
    scan_results = suggestions_state.get('scan_results', [])
    cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
    no_match_abs_ids = suggestions_state.get('scan_cache_no_match_abs_ids', []) or []
    if ignored_source_ids:
        filtered_results = [
            item for item in scan_results
            if (item.get('bridge_key') or item.get('abs_id')) not in ignored_source_ids
        ]
        filtered_cache_by_abs = {
            abs_id: suggestion for abs_id, suggestion in cache_by_abs.items()
            if abs_id not in ignored_source_ids
        }
        filtered_no_match_abs_ids = [abs_id for abs_id in no_match_abs_ids if abs_id not in ignored_source_ids]
        if len(filtered_results) != len(scan_results):
            suggestions_state['scan_results'] = filtered_results
            scan_results = filtered_results
            suggestions_state['updated_at'] = time.time()
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['updated_at'] = time.time()
        if len(filtered_no_match_abs_ids) != len(no_match_abs_ids):
            suggestions_state['scan_cache_no_match_abs_ids'] = filtered_no_match_abs_ids
            no_match_abs_ids = filtered_no_match_abs_ids
            suggestions_state['updated_at'] = time.time()
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
            "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
            "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
        })

    active_suggestion_keys = set()
    for book in database_service.get_all_books():
        abs_id = str(getattr(book, 'abs_id', '') or '').strip()
        if abs_id:
            active_suggestion_keys.add(abs_id)
            if abs_id.lower().startswith("booklore_audio_"):
                legacy_source_id = abs_id.split("_", 2)[-1].strip()
                legacy_bridge = _build_bridge_key("BookLore", legacy_source_id)
                if legacy_bridge:
                    active_suggestion_keys.add(legacy_bridge)

        mapped_bridge = _build_bridge_key(
            getattr(book, 'audio_source', None),
            getattr(book, 'audio_source_id', None),
        )
        if mapped_bridge:
            active_suggestion_keys.add(mapped_bridge)

    if active_suggestion_keys:
        filtered_results = [
            item for item in scan_results
            if (item.get('bridge_key') or item.get('abs_id')) not in active_suggestion_keys
        ]
        filtered_cache_by_abs = {
            key: suggestion for key, suggestion in cache_by_abs.items()
            if key not in active_suggestion_keys
        }
        filtered_no_match_abs_ids = [
            key for key in no_match_abs_ids if key not in active_suggestion_keys
        ]
        if len(filtered_results) != len(scan_results):
            suggestions_state['scan_results'] = filtered_results
            scan_results = filtered_results
            suggestions_state['updated_at'] = time.time()
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['updated_at'] = time.time()
        if len(filtered_no_match_abs_ids) != len(no_match_abs_ids):
            suggestions_state['scan_cache_no_match_abs_ids'] = filtered_no_match_abs_ids
            no_match_abs_ids = filtered_no_match_abs_ids
            suggestions_state['updated_at'] = time.time()
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
            "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
            "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
        })

    def _normalize_suggestion_identity_part(value):
        normalized = re.sub(r'[\W_]+', ' ', str(value or '').lower()).strip()
        return normalized

    deduped_results = []
    seen_identity = {}
    removed_duplicate_keys = []
    for item in scan_results:
        suggestion_key = (item.get('bridge_key') or item.get('abs_id') or '').strip()
        source = (item.get('audio_source') or _audio_source_from_bridge_key(suggestion_key) or 'ABS').strip().lower()
        title = _normalize_suggestion_identity_part(item.get('audio_title') or item.get('abs_title'))
        author = _normalize_suggestion_identity_part(item.get('audio_author') or item.get('abs_author'))
        if not title:
            dedupe_key = ('key', suggestion_key)
        else:
            dedupe_key = (source, title, author)

        if dedupe_key in seen_identity:
            removed_duplicate_keys.append(suggestion_key)
            continue

        seen_identity[dedupe_key] = suggestion_key
        deduped_results.append(item)

    if removed_duplicate_keys:
        removed_set = set(removed_duplicate_keys)
        scan_results = deduped_results
        suggestions_state['scan_results'] = deduped_results
        filtered_cache_by_abs = {
            key: suggestion for key, suggestion in cache_by_abs.items()
            if key not in removed_set
        }
        if len(filtered_cache_by_abs) != len(cache_by_abs):
            cache_by_abs = filtered_cache_by_abs
            suggestions_state['scan_cache_by_abs'] = filtered_cache_by_abs
        suggestions_state['updated_at'] = time.time()
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
            "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
            "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
        })

    return render_template(
        'suggestions.html',
        suggestions=_sanitize_cover_urls(scan_results),
        queue=_sanitize_cover_urls(_load_match_queue()),
        scan_has_run=bool(suggestions_state.get('scan_has_run', False)),
        scan_in_progress=scan_in_progress,
        scan_error=scan_error,
        scan_stats=suggestions_state.get('scan_last_stats', {}),
        storyteller_enabled=storyteller_enabled,
    )


def suggestions_scan_status():
    _clear_legacy_suggestions_session_payload()
    job_id = session.get('suggestions_scan_job_id')
    if not job_id:
        return jsonify({"status": "idle"})

    scan_job = _get_suggestions_scan_job(job_id)
    if not scan_job:
        session.pop('suggestions_scan_job_id', None)
        session.modified = True
        return jsonify({"status": "idle"})

    response = {
        "status": scan_job.get('status', 'idle'),
        "error": scan_job.get('error'),
        "progress": scan_job.get('progress', {}),
    }
    if scan_job.get('status') == 'done':
        result_payload = scan_job.get('results', {}) or {}
        response["count"] = len(result_payload.get('suggestions', []))
        response["stats"] = result_payload.get('stats', {})

    return jsonify(response)


def cleanup_mapping_resources(book, defer_audio_cache: bool = False):
    """Delete external artifacts and membership data for a mapped book."""
    if not book:
        return
    clients = uc()

    try:
        remaining_books = database_service.get_all_books()
    except Exception as e:
        logger.warning(
            "Failed to check remaining mappings during resource cleanup; "
            "preserving shared Storyteller resources: %s",
            e,
            exc_info=True,
        )
        remaining_books = None

    remaining_storyteller_uuids = set()
    remaining_cache_filenames = set()
    if remaining_books is not None:
        for remaining_book in remaining_books:
            remaining_filename = getattr(remaining_book, 'ebook_filename', None)
            if remaining_filename:
                remaining_cache_filenames.add(remaining_filename)
            remaining_local_filename = local_ebook_filename(remaining_book)
            if remaining_local_filename:
                remaining_cache_filenames.add(remaining_local_filename)

            remaining_uuid = getattr(remaining_book, 'storyteller_uuid', None)
            if not remaining_uuid and remaining_filename:
                match = re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", remaining_filename)
                if match:
                    remaining_uuid = match.group(1)
            if remaining_uuid:
                remaining_storyteller_uuids.add(remaining_uuid)
                remaining_cache_filenames.add(f"storyteller_{remaining_uuid}.epub")

    if book.transcript_file:
        try:
            Path(book.transcript_file).unlink()
        except Exception:
            pass

    # Clean up audio cache directory (WAV files from whisper transcription)
    audio_cache_dir = DATA_DIR / "audio_cache" / book.abs_id
    if audio_cache_dir.exists() and not defer_audio_cache:
        try:
            shutil.rmtree(audio_cache_dir)
            logger.info(f"🗑️ Deleted audio cache: {audio_cache_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete audio cache: {e}", exc_info=True)

    # Clean up full transcript directory (chapter JSON files + manifest)
    transcript_dir = DATA_DIR / "transcripts" / "storyteller" / book.abs_id
    if transcript_dir.exists():
        try:
            shutil.rmtree(transcript_dir)
            logger.info(f"🗑️ Deleted transcript directory: {transcript_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete transcript directory: {e}", exc_info=True)

    cached_ebook_filename = local_ebook_filename(book)
    preserve_cached_ebook = (
        remaining_books is None
        or cached_ebook_filename in remaining_cache_filenames
    )
    if cached_ebook_filename and not preserve_cached_ebook:
        cache_dirs = []
        try:
            cache_dirs.append(container.epub_cache_dir())
        except Exception:
            pass

        manager_cache_dir = getattr(manager, 'epub_cache_dir', None)
        if manager_cache_dir:
            cache_dirs.append(manager_cache_dir)

        seen_dirs = set()
        for cache_dir in cache_dirs:
            cache_dir_path = Path(cache_dir)
            cache_dir_key = str(cache_dir_path)
            if cache_dir_key in seen_dirs:
                continue
            seen_dirs.add(cache_dir_key)

            cached_path = safe_cache_path(cache_dir_path, cached_ebook_filename)
            if cached_path and cached_path.exists():
                try:
                    cached_path.unlink()
                    logger.info(f"🗑️ Deleted cached ebook file: {cached_ebook_filename}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete cached ebook {cached_ebook_filename}: {e}", exc_info=True)

    # KoSync progress must not outlive the mapping. The document hash comes from
    # the EPUB's content, so re-matching the same file re-links the identical hash
    # and the furthest-wins gate serves the pre-delete position back against the
    # fresh book's empty state (#358). This applies to every sync mode — it was
    # previously done for ebook-only mappings alone.
    try:
        docs_deleted, progress_deleted = database_service.delete_kosync_data_for_book(book.abs_id)
        if docs_deleted or progress_deleted:
            logger.info(
                f"🗑️ Deleted KOSync data for mapping '{book.abs_id}': "
                f"{docs_deleted} document(s), {progress_deleted} progress row(s)"
            )
    except Exception as e:
        logger.warning(f"⚠️ Failed to delete KOSync data for '{book.abs_id}': {e}", exc_info=True)

    # A deleted mapping is the user's explicit "re-match this" signal, so the
    # shelf-watch re-scan throttle must not outlive it either: the throttle row
    # is keyed by the library's book id, which survives both the delete and a
    # rename in the source library, so re-adding the book to the watch shelf
    # would otherwise be skipped as `skipped_throttled` for the whole rescan
    # window.
    from src.services.shelf_watch_service import clear_shelf_watch_throttle
    clear_shelf_watch_throttle(database_service, book)

    is_abs_backed = (
        getattr(book, 'sync_mode', 'audiobook') != 'ebook_only'
        and not str(book.abs_id).startswith('booklore:')
    )
    if is_abs_backed:
        collection_name = user_setting('ABS_COLLECTION_NAME', 'Synced with KOReader')
        try:
            clients.abs_client.remove_from_collection(book.abs_id, collection_name)
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove from ABS collection: {e}", exc_info=True)
    else:
        logger.info(f"Skipping ABS collection cleanup for non-ABS mapping '{book.abs_id}'")

    storyteller_uuid = getattr(book, 'storyteller_uuid', None)
    if not storyteller_uuid and getattr(book, 'ebook_filename', None):
        match = re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
        if match:
            storyteller_uuid = match.group(1)
            logger.info(f"Inferred Storyteller UUID for cleanup: '{storyteller_uuid[:8]}...'")

    preserve_storyteller_link = (
        remaining_books is None
        or storyteller_uuid in remaining_storyteller_uuids
    )
    if storyteller_uuid and not preserve_storyteller_link:
        storyteller_collection_name = os.environ.get('STORYTELLER_COLLECTION_NAME', 'Synced with KOReader')
        try:
            st_client = clients.storyteller_client
            if hasattr(st_client, 'remove_from_collection_by_uuid'):
                removed = st_client.remove_from_collection_by_uuid(storyteller_uuid, storyteller_collection_name)
                if not removed:
                    logger.warning(f"Storyteller collection removal returned no success for '{storyteller_uuid[:8]}...'")
            else:
                logger.warning("Storyteller client has no remove_from_collection_by_uuid method")
        except Exception as e:
            logger.warning(f"Failed to remove from Storyteller collection: {e}", exc_info=True)
    elif storyteller_uuid:
        logger.info(
            f"Preserving shared Storyteller resources for '{storyteller_uuid[:8]}...': "
            "another mapping still references them"
        )

    if book.ebook_filename:
        shelf_filename = book.original_ebook_filename or book.ebook_filename
        is_bookorbit = (getattr(book, 'ebook_source', None) or '').strip().lower() == 'bookorbit'
        is_kavita = (getattr(book, 'ebook_source', None) or '').strip().lower() == 'kavita'
        try:
            if is_bookorbit:
                client = clients.bookorbit_client
                if client.is_configured():
                    shelf_name = (user_setting('BOOKORBIT_SHELF_NAME', 'Kobo') or 'Kobo').strip()
                    ebook_source_id = getattr(book, 'ebook_source_id', None)
                    if ebook_source_id and hasattr(client, 'remove_book_id_from_shelf'):
                        client.remove_book_id_from_shelf(ebook_source_id, shelf_name)
                    else:
                        client.remove_from_shelf(shelf_filename, shelf_name)
            elif is_kavita:
                client = clients.kavita_client
                if client.is_configured():
                    shelf_name = (user_setting('KAVITA_COLLECTION_NAME', 'BookBridge') or 'BookBridge').strip()
                    ebook_source_id = getattr(book, 'ebook_source_id', None)
                    if ebook_source_id:
                        client.remove_book_id_from_shelf(ebook_source_id, shelf_name)
                    else:
                        client.remove_from_shelf(shelf_filename, shelf_name)
            else:
                client = clients.booklore_client
                if client.is_configured():
                    shelf_name = user_setting('BOOKLORE_SHELF_NAME', 'Kobo')
                    client.remove_from_shelf(shelf_filename, shelf_name)
        except Exception as e:
            source_label = 'BookOrbit' if is_bookorbit else ('Kavita' if is_kavita else 'Grimmory')
            logger.warning(f"⚠️ Failed to remove from {source_label} shelf: {e}", exc_info=True)


def _user_may_modify_book(user, abs_id) -> bool:
    """A user may delete/clear/complete a book only if they are an admin or have
    claimed it (user_books link). Prevents one user from destroying or resetting
    another user's mapping/progress."""
    if current_app.config.get('LOGIN_DISABLED'):
        return True  # auth disabled (tests / explicit single-user)
    if user is None:
        return False
    if getattr(user, "is_admin", False):
        return True
    try:
        return database_service.is_user_linked(user.id, abs_id)
    except Exception:
        return False


def _forbidden_book_response(json_response: bool = False):
    message = "Forbidden: you have not claimed this book"
    if json_response or _request_wants_json():
        return jsonify({"success": False, "error": message}), 403
    return (message, 403)


def _current_user_claimed_abs_ids(user) -> set:
    if current_app.config.get('LOGIN_DISABLED'):
        return {
            getattr(book, "abs_id", None)
            for book in database_service.get_all_books()
            if getattr(book, "abs_id", None)
        }
    if user is None:
        return set()
    try:
        return set(database_service.get_linked_abs_ids(user.id) or set())
    except Exception:
        return set()


def _kosync_document_visible_to_user(doc, user, claimed_abs_ids: set) -> bool:
    if doc is None:
        return False
    if current_app.config.get('LOGIN_DISABLED'):
        return True
    if user is None:
        return False
    if getattr(doc, "user_id", None) == user.id:
        return True
    linked_abs_id = getattr(doc, "linked_abs_id", None)
    return bool(linked_abs_id and linked_abs_id in claimed_abs_ids)


def _book_claimed_by_current_scope(abs_id: str) -> bool:
    if current_app.config.get('LOGIN_DISABLED'):
        return True
    user = current_user()
    if user is None or not abs_id:
        return False
    try:
        return database_service.is_user_linked(user.id, abs_id)
    except Exception:
        return False


def _serialize_kosync_document_for_ui(doc, linked_book=None) -> dict:
    return {
        "document_hash": doc.document_hash,
        "progress": doc.progress,
        "percentage": float(doc.percentage) if doc.percentage else 0,
        "device": doc.device,
        "device_id": doc.device_id,
        "timestamp": doc.timestamp.isoformat() if doc.timestamp else None,
        "first_seen": doc.first_seen.isoformat() if doc.first_seen else None,
        "last_updated": doc.last_updated.isoformat() if doc.last_updated else None,
        "linked_abs_id": doc.linked_abs_id,
        "linked_book_title": linked_book.abs_title if linked_book else None,
        "filename": doc.filename,
        "source": doc.source,
        "owned_by_current_user": bool(
            current_app.config.get('LOGIN_DISABLED')
            or (current_user() is not None and doc.user_id == current_user().id)
        ),
    }


def api_me_kosync_documents():
    """User-scoped KOSync document list for the dashboard modal."""
    user = current_user()
    claimed_abs_ids = _current_user_claimed_abs_ids(user)
    visible_docs = []
    for doc in database_service.get_all_kosync_documents():
        if not _kosync_document_visible_to_user(doc, user, claimed_abs_ids):
            continue
        linked_book = database_service.get_book(doc.linked_abs_id) if doc.linked_abs_id else None
        visible_docs.append(_serialize_kosync_document_for_ui(doc, linked_book=linked_book))

    return jsonify({
        "documents": visible_docs,
        "total": len(visible_docs),
        "linked": sum(1 for d in visible_docs if d["linked_abs_id"]),
        "unlinked": sum(1 for d in visible_docs if not d["linked_abs_id"]),
    })


def api_me_books():
    """Return current user's claimed BookBridge books as KOSync link targets."""
    user = current_user()
    if current_app.config.get('LOGIN_DISABLED'):
        books = database_service.get_all_books()
    elif user is None:
        books = []
    else:
        books = database_service.get_all_books(user_id=user.id)

    result = []
    for book in books or []:
        result.append({
            "abs_id": book.abs_id,
            "title": book.abs_title,
            "author": getattr(book, "audio_title", None) or "",
            "ebook_filename": book.ebook_filename,
            "kosync_doc_id": book.kosync_doc_id,
        })
    result.sort(key=lambda b: (b.get("title") or "").lower())
    return jsonify({"books": result, "total": len(result)})


def _get_visible_kosync_document_or_response(doc_hash: str):
    doc = database_service.get_kosync_document(doc_hash)
    if not doc:
        return None, (jsonify({"success": False, "error": "This KOSync document is no longer available."}), 404)
    user = current_user()
    claimed_abs_ids = _current_user_claimed_abs_ids(user)
    if not _kosync_document_visible_to_user(doc, user, claimed_abs_ids):
        return None, (jsonify({"success": False, "error": "You do not have permission to change this document."}), 403)
    return doc, None


def api_me_link_kosync_document(doc_hash):
    """Link a visible device hash to one of the current user's claimed books."""
    doc, error = _get_visible_kosync_document_or_response(doc_hash)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    abs_id = str(data.get("abs_id") or "").strip()
    if not abs_id:
        return jsonify({"success": False, "error": "Missing book selection."}), 400

    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "That book is not in your library."}), 404

    if not _book_claimed_by_current_scope(abs_id):
        return jsonify({"success": False, "error": "That book is not in your library."}), 403

    success = database_service.link_kosync_document(doc.document_hash, abs_id)
    if not success:
        return jsonify({"success": False, "error": "Could not link this KOSync document."}), 500

    database_service.dismiss_suggestion(doc.document_hash)
    linked = database_service.get_kosync_document(doc.document_hash)
    return jsonify({
        "success": True,
        "message": f"Linked to {book.abs_title}",
        "document": _serialize_kosync_document_for_ui(linked, linked_book=book),
    })


def api_me_unlink_kosync_document(doc_hash):
    """Clear the book link for a visible KOSync document."""
    doc, error = _get_visible_kosync_document_or_response(doc_hash)
    if error:
        return error
    if not doc.linked_abs_id:
        return jsonify({"success": True, "message": "Document is already unlinked."})
    if not _book_claimed_by_current_scope(doc.linked_abs_id):
        return jsonify({"success": False, "error": "You do not have permission to change this document."}), 403

    success = database_service.unlink_kosync_document(doc.document_hash)
    if not success:
        return jsonify({"success": False, "error": "Could not unlink this KOSync document."}), 500
    updated = database_service.get_kosync_document(doc.document_hash)
    return jsonify({
        "success": True,
        "message": "Document unlinked.",
        "document": _serialize_kosync_document_for_ui(updated),
    })


def api_me_delete_kosync_document(doc_hash):
    """Delete an unlinked KOSync document owned by the current user."""
    doc, error = _get_visible_kosync_document_or_response(doc_hash)
    if error:
        return error
    if doc.linked_abs_id:
        return jsonify({"success": False, "error": "Unlink this document before deleting it."}), 400
    if not current_app.config.get('LOGIN_DISABLED'):
        user = current_user()
        if user is None or doc.user_id != user.id:
            return jsonify({"success": False, "error": "You do not have permission to delete this document."}), 403

    success = database_service.delete_kosync_document(doc.document_hash)
    if not success:
        return jsonify({"success": False, "error": "Could not delete this KOSync document."}), 500
    return jsonify({"success": True, "message": "Document deleted."})


def _delete_or_unlink_book(user, abs_id, book) -> None:
    """Shared catalog: drop the user's claim (+ their progress) when other users
    still claim the book; otherwise fully delete it."""
    claimants = database_service.get_book_user_ids(abs_id)
    if user is not None and user.id in claimants and len(claimants) > 1:
        database_service.unlink_user_book(user.id, abs_id)
        database_service.delete_states_for_book(abs_id, user_id=user.id)
        return
    worker_cancelled = manager.cancel_background_job(abs_id)
    database_service.delete_book(abs_id)
    cleanup_mapping_resources(book, defer_audio_cache=worker_cancelled)


def delete_mapping(abs_id):
    book = database_service.get_book(abs_id)
    if not book:
        return redirect(url_for('index'))

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return ("Forbidden: you have not claimed this book", 403)

    _delete_or_unlink_book(user, abs_id, book)
    return redirect(url_for('index'))


def clear_progress(abs_id):
    """Clear progress for a mapping by setting all systems to 0%"""
    # Get book from database service
    book = database_service.get_book(abs_id)

    if not book:
        logger.warning(f"⚠️ Cannot clear progress: book not found for '{abs_id}'")
        return redirect(url_for('index'))

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return ("Forbidden: you have not claimed this book", 403)

    try:
        # Scope to the acting user: drop only their state rows and reset progress
        # through their own clients, never the global/admin bundle.
        logger.info(f"🔄 Clearing progress for {sanitize_log_data(book.abs_title or abs_id)}")
        manager.clear_progress(
            abs_id,
            user_id=(user.id if user else None),
            sync_clients=uc().sync_clients,
        )
        logger.info(f"✅ Progress cleared successfully for {sanitize_log_data(book.abs_title or abs_id)}")

    except Exception as e:
        logger.error(f"❌ Failed to clear progress for '{abs_id}': {e}", exc_info=True)

    return redirect(url_for('index'))


def remap_alignment(abs_id):
    """Rebuild a book's audio↔text alignment with the best available backend.

    Unlike Clear Position, Remap never touches the reader's saved progress — it only
    rebuilds the audio↔ebook map:
      - CTC configured and the current map is not already CTC -> rebuild with CTC.
      - otherwise an estimated map (no measured word timings) -> force a fresh
        word-timestamped transcription and re-anchor.
      - a map already at the best available backend -> nothing to do.

    The rebuild runs through the normal pending -> forge pipeline, exactly like
    ``/api/alignments/realign`` (single scope).
    """
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)

    # Audio→text alignment only exists for mappings that have an audiobook side.
    if getattr(book, "sync_mode", "audiobook") == "ebook_only":
        return jsonify({
            "success": False,
            "error": "This mapping has no audiobook to align.",
        }), 400

    # None = no stored map; "" = map with unrecorded (legacy) method.
    current_method = database_service.get_alignment_method(abs_id)
    ctc_available = env_truthy("CTC_ENABLED")

    # Methods that are already word-accurate and need no word-level rebuild. Coarse
    # fallbacks ('lexical', 'linear', 'llm_anchor', 'storyteller[_linear]', legacy '')
    # are all improvable, so they are deliberately absent here.
    _word_accurate = ("ctc", "lexical_timed")

    target = None
    if ctc_available and current_method != "ctc":
        target = "ctc"
    elif (current_method or "") not in _word_accurate:
        target = "word_level"

    if target is None:
        return jsonify({
            "success": False,
            "status": "up_to_date",
            "message": "Alignment already uses the best available backend.",
        })

    # A rebuild reuses the cached transcript to skip Whisper; a transcript captured
    # before word-level timing has no per-word times, so drop it to force a fresh,
    # word-timestamped transcription. (CTC ignores the transcript entirely.)
    if target == "word_level":
        transcriber = getattr(manager, "transcriber", None) if manager else None
        if transcriber is not None:
            try:
                transcriber.invalidate_transcript_cache(abs_id)
            except Exception as e:
                logger.warning(
                    f"⚠️ Remap: could not invalidate transcript for '{abs_id}': {e}",
                    exc_info=True,
                )

    if not database_service.set_book_status(abs_id, "pending"):
        return jsonify({"success": False, "error": "Could not queue remap."}), 500

    logger.info(
        f"🔁 Remap queued for {sanitize_log_data(book.abs_title or abs_id)} "
        f"(backend='{target}', from='{current_method or 'none'}')"
    )
    return jsonify({"success": True, "backend": target})



def sync_now(abs_id):
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)

    if user is not None and not current_app.config.get('LOGIN_DISABLED'):
        threading.Thread(
            target=manager.sync_cycle,
            kwargs={'target_abs_id': abs_id, 'user_id': user.id},
            daemon=True,
        ).start()
    else:
        threading.Thread(target=manager.run_sync_for_all_users, kwargs={'target_abs_id': abs_id}, daemon=True).start()
    return jsonify({"success": True})

def mark_complete(abs_id):
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return jsonify({"success": False, "error": "Forbidden: you have not claimed this book"}), 403

    perform_delete = request.json.get('delete', False) if request.json else False

    # Determine applicable sync type from the book's sync_mode.
    sync_type = 'ebook' if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' else 'audiobook'

    locator = LocatorResult(percentage=1.0)
    update_req = UpdateProgressRequest(locator_result=locator, txt="Book finished", previous_location=None)

    # Push through the acting user's own clients (not the global/admin bundle) so a
    # user's "finished" never lands on another account's trackers.
    # Follow the same applicability pattern as SyncManager.clear_progress:
    # configured + matching sync_type + supports_book.
    for client_name, client in uc().sync_clients.items():
        if not client.is_configured():
            continue
        # Skip if this client does not handle the book's sync type.
        if sync_type not in client.get_supported_sync_types():
            continue
        # Skip if the client cannot handle this book's specific source.
        if not client.supports_book(book):
            continue

        success = False
        result = None
        updated_state = {}
        if client_name.lower() == 'abs':
            try:
                success = bool(client.abs_client.mark_finished(abs_id))
                if success:
                    # ABS's isFinished flag does not necessarily move currentTime
                    # to the exact duration. Persist audio-position seconds here,
                    # never wall-clock epoch seconds, because ABSSyncClient uses
                    # State.timestamp as its previous audio position.
                    duration = getattr(book, 'duration', None)
                    updated_state = {
                        'pct': 1.0,
                        'ts': float(duration) if duration and duration > 0 else 0.0,
                    }
            except Exception as e:
                logger.error(f"❌ ABS mark_finished failed for '{abs_id}': {e}", exc_info=True)
        else:
            try:
                result = client.update_progress(book, update_req)
                success = getattr(result, 'success', False) if result else False
                if success:
                    updated_state = getattr(result, 'updated_state', {}) or {}
            except Exception as e:
                logger.error(f"❌ '{client_name}' mark-complete failed for '{abs_id}': {e}", exc_info=True)

        # Only persist state when the write succeeded.
        if success:
            # Preserve locator/audio metadata returned by the client. Percentage
            # clients historically use State.timestamp as an observation epoch;
            # audio clients return a real position in updated_state['ts'].
            now = int(time.time())
            resolved_timestamp = updated_state.get('ts')
            state_timestamp = resolved_timestamp if resolved_timestamp is not None else now
            state = State(
                abs_id=abs_id,
                client_name=client_name.lower(),
                percentage=1.0,
                timestamp=state_timestamp,
                last_updated=now,
                user_id=(user.id if user else None),
                xpath=updated_state.get('xpath'),
                cfi=updated_state.get('cfi'),
            )
            database_service.save_state(state)

    if perform_delete:
        _delete_or_unlink_book(user, abs_id, book)

    return jsonify({"success": True})

def update_hash(abs_id):
    from flask import flash
    new_hash = request.form.get('new_hash', '').strip()
    book = database_service.get_book(abs_id)

    if not book:
        flash("❌ Book not found", "error")
        return redirect(url_for('index'))

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response()
    claimants = database_service.get_book_user_ids(abs_id)
    if (
        user is not None
        and not user.is_admin
        and len(claimants) > 1
    ):
        return ("Forbidden: only an administrator can change a shared book hash", 403)

    old_hash = book.kosync_doc_id

    if new_hash:
        book.kosync_doc_id = new_hash
        database_service.save_book(book)
        logger.info(f"✅ Updated KoSync hash for '{sanitize_log_data(book.abs_title)}' to manual input: '{new_hash}'")
        updated = True
    else:
        # Auto-regenerate
        # When recalculating with empty input, prioritize the standard EPUB.
        # over the current filename (which might be a Storyteller artifact).
        target_filename = book.original_ebook_filename or book.ebook_filename
        
        booklore_id = None
        booklore_client = uc().booklore_client
        if booklore_client.is_configured():
            bl_book = booklore_client.find_book_by_filename(target_filename)
            if bl_book:
                booklore_id = bl_book.get('id')

        recalc_hash = get_kosync_id_for_ebook(target_filename, booklore_id, original_filename=book.ebook_filename)
        
        if recalc_hash:
            # [CHANGED] Manual update (via UI) should always succeed, even if it changes a linked hash.
            # The protection logic remains in match() and batch_match() to prevent automated overwrites.
            book.kosync_doc_id = recalc_hash
            database_service.save_book(book)
            logger.info(f"✅ Auto-regenerated KoSync hash for '{sanitize_log_data(book.abs_title)}': '{recalc_hash}'")
            updated = True
        else:
            flash("❌ Could not recalculate hash (file not found?)", "error")
            return redirect(url_for('index'))

    # Keep both sides of a hash change resolvable. The selected hash remains the
    # book's primary pointer, while the previous hash stays linked as a sibling so
    # existing devices and progress rows are never stranded.
    hashes_to_link = [book.kosync_doc_id]
    if old_hash and old_hash != book.kosync_doc_id:
        hashes_to_link.append(old_hash)
    for doc_hash in hashes_to_link:
        if not updated or not doc_hash:
            continue
        try:
            database_service.ensure_linked_kosync_document(doc_hash, abs_id)
        except Exception as e:
            logger.warning(
                f"⚠️ Could not register linked KoSync document '{sanitize_log_data(doc_hash)}' "
                f"for '{sanitize_log_data(book.abs_title)}': {e}",
                exc_info=True
            )

    # Trigger an instant sync cycle so the engine can reconcile progress
    # using 'furthest wins' logic. This avoids overwriting newer progress
    # that may already exist on the KOSync server (e.g., from BookNexus).
    if updated and book.kosync_doc_id != old_hash:
        logger.info(f"🔄 Hash changed for '{sanitize_log_data(book.abs_title)}' — triggering instant sync to reconcile progress")
        sync_kwargs = {'target_abs_id': abs_id}
        if user is not None and not current_app.config.get('LOGIN_DISABLED'):
            sync_kwargs['user_id'] = user.id
        threading.Thread(target=manager.sync_cycle, kwargs=sync_kwargs, daemon=True).start()

    flash(f"✅ Updated KoSync Hash for {book.abs_title}", "success")
    return redirect(url_for('index'))


def _extracted_cover_is_stale(cover_path, source_path) -> bool:
    """True when the ebook has been rewritten since its cover was extracted.

    Adding or replacing a cover rewrites the EPUB, but the extracted jpg is
    written once and served forever, so the dashboard keeps showing the old
    art (and it is preferred over the live audio cover). Compare mtimes so an
    edited book re-extracts on the next request.
    """
    try:
        return Path(source_path).stat().st_mtime > Path(cover_path).stat().st_mtime
    except (OSError, TypeError):
        return False


_COVER_FRESH_TTL_SECONDS = 300
_cover_fresh_until: dict = {}

# Browser-side cache window for extracted covers. Flask leaves `max_age` unset by
# default, which sends `Cache-Control: no-cache` — the browser then revalidates
# EVERY cover on EVERY dashboard load, so a large library pays hundreds of
# conditional round trips just to be told 304. Covers are auth-gated, hence
# `private`. The window deliberately matches _COVER_FRESH_TTL_SECONDS: a re-extracted
# cover is already allowed to be up to that stale server-side, so the browser cache
# adds no staleness the server does not already accept.
_COVER_BROWSER_MAX_AGE_SECONDS = _COVER_FRESH_TTL_SECONDS


def _send_cover_cached(filename: str):
    """Serve an extracted cover with a browser cache window (see the constant)."""
    response = send_from_directory(
        COVERS_DIR, filename, max_age=_COVER_BROWSER_MAX_AGE_SECONDS
    )
    response.headers['Cache-Control'] = (
        f'private, max-age={_COVER_BROWSER_MAX_AGE_SECONDS}'
    )
    return response


def serve_cover(filename):
    """Serve cover images with lazy extraction."""
    # Filename is likely <hash>.jpg
    doc_hash = filename.replace('.jpg', '')

    # 1. Check if file exists
    cover_path = COVERS_DIR / filename

    # The dashboard requests one cover per mapping, so anything on this path runs
    # hundreds of times per page load. Confirming freshness costs a DB lookup plus
    # resolve_book_path, which falls back to an rglob of the whole library when its
    # 100-entry path cache misses — and a large library has far more books than
    # that. Remember a recent "fresh" verdict so that work happens at most once per
    # book per TTL instead of on every request; an ebook edited inside the window is
    # picked up on the next one.
    if cover_path.exists() and _cover_fresh_until.get(doc_hash, 0) > time.time():
        return _send_cover_cached(filename)

    book = database_service.get_book_by_kosync_id(doc_hash)

    if cover_path.exists():
        source_path = None
        if book and book.ebook_filename:
            try:
                source_path = container.ebook_parser().resolve_book_path(book.ebook_filename)
            except Exception as e:
                logger.debug(f"Cover freshness check could not resolve the ebook: {e}")
        if not source_path or not _extracted_cover_is_stale(cover_path, source_path):
            _cover_fresh_until[doc_hash] = time.time() + _COVER_FRESH_TTL_SECONDS
            return _send_cover_cached(filename)
        _cover_fresh_until.pop(doc_hash, None)
        logger.info(
            "🖼️ Re-extracting cover for '%s': the ebook changed since it was cached",
            sanitize_log_data(getattr(book, "abs_title", None) or doc_hash),
        )

    # 2. Try to extract

    if book and book.ebook_filename:
        # We need the full path to the book. ebook_parser resolves it usually.
        # extract_cover expects a path or filename that can be resolved.
        # Let's pass what we have.
        try:
             # Find actual file path using EbookParser resolution if needed,
             # but extract_cover in my implementation takes 'filepath' and calls Path(filepath).
             # If book.ebook_filename is just a name, we might need to resolve it.
             # container.ebook_parser().resolve_book_path(book.ebook_filename)

             # Actually, let's let EbookParser handle resolution or pass full path if we know it.
             # EbookParser.extract_cover currently does `Path(filepath)`.
             # It doesn't call `resolve_book_path` internally in the code I wrote?
             # Let's double check my implementation of extract_cover.
             # I wrote: `filepath = Path(filepath); book = epub.read_epub(str(filepath))`
             # So it expects a valid path. I should resolve it first.

             parser = container.ebook_parser()
             full_book_path = parser.resolve_book_path(book.ebook_filename)

             if parser.extract_cover(full_book_path, cover_path):
                 return _send_cover_cached(filename)
        except Exception as e:
            logger.debug(f"Lazy cover extraction failed: {e}")

    return "Cover not found", 404

def api_storyteller_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    results = uc().storyteller_client.search_books(query)
    return jsonify(results)


def api_storyteller_link(abs_id):
    data = request.get_json()
    if not data or 'uuid' not in data:
        return jsonify({"error": "Missing 'uuid' in JSON payload"}), 400

    storyteller_uuid = (data['uuid'] or '').strip()
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"error": "Book not found"}), 404

    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)

    # Handle explicit unlinking.
    if storyteller_uuid == "none" or not storyteller_uuid:
        logger.info(f"🔄 Unlinking Storyteller for '{book.abs_title}'")
        previous_storyteller_uuid = book.storyteller_uuid
        if not previous_storyteller_uuid and getattr(book, 'ebook_filename', None):
            match = re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
            if match:
                previous_storyteller_uuid = match.group(1)
                logger.info(f"Inferred Storyteller UUID for unlink: '{previous_storyteller_uuid[:8]}...'")
        book.storyteller_uuid = None
        book.transcript_source = None
        book.transcript_file = None

        if previous_storyteller_uuid:
            try:
                st_client = uc().storyteller_client
                if hasattr(st_client, 'remove_from_collection_by_uuid'):
                    storyteller_collection_name = os.environ.get('STORYTELLER_COLLECTION_NAME', 'Synced with KOReader')
                    removed = st_client.remove_from_collection_by_uuid(previous_storyteller_uuid, storyteller_collection_name)
                    if not removed:
                        logger.warning(f"Storyteller unlink removal returned no success for '{previous_storyteller_uuid[:8]}...'")
                else:
                    logger.warning("Storyteller client has no remove_from_collection_by_uuid method")
            except Exception as e:
                logger.warning(f"Failed to remove Storyteller UUID from collection: {e}", exc_info=True)
        
        # Revert to original filename if it exists
        if book.original_ebook_filename:
            book.ebook_filename = book.original_ebook_filename
        if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only':
            book.sync_mode = 'ebook_only'

        book.status = 'pending'
        database_service.save_book(book)
        
        return jsonify({"message": "Storyteller unlinked successfully", "filename": book.ebook_filename}), 200

    try:
        source_filename = book.original_ebook_filename
        if not source_filename and book.ebook_filename and not _is_storyteller_artifact_filename(book.ebook_filename):
            source_filename = book.ebook_filename

        saved_book, err_msg, err_code = _upsert_storyteller_mapping(
            mode_hint="existing",
            abs_id=abs_id,
            abs_title=book.abs_title or '',
            storyteller_uuid=storyteller_uuid,
            ebook_filename=source_filename,
            existing_book=book,
            duration=book.duration,
        )
        if err_msg:
            return jsonify({"error": err_msg}), err_code

        _shelve_saved_ebook(saved_book)
        return jsonify({"message": "Book linked successfully", "filename": saved_book.ebook_filename}), 200
    except Exception as e:
        logger.error(f"❌ Error linking Storyteller book for '{abs_id}': {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def _get_stats_timezone():
    tz_name = os.environ.get("TZ", "America/New_York") or "America/New_York"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("Invalid TZ '%s' for stats, falling back to America/New_York", tz_name, exc_info=True)
        return ZoneInfo("America/New_York")


def _build_stats_cache_key():
    return f"stats::{os.environ.get('TZ', 'America/New_York')}"


def _read_cached_stats():
    cache_key = _build_stats_cache_key()
    with STATS_CACHE_LOCK:
        entry = STATS_CACHE.get(cache_key)
        if not entry:
            return None
        if (time.time() - entry["created_at"]) > STATS_CACHE_TTL_SECONDS:
            STATS_CACHE.pop(cache_key, None)
            return None
        return entry["payload"]


def _write_cached_stats(payload):
    cache_key = _build_stats_cache_key()
    with STATS_CACHE_LOCK:
        STATS_CACHE[cache_key] = {
            "created_at": time.time(),
            "payload": payload,
        }


def _date_series(start_date, end_date):
    values = []
    cursor = start_date
    while cursor <= end_date:
        values.append(cursor)
        cursor += timedelta(days=1)
    return values


def _activity_dates_from_daily(daily):
    dates = set()
    for row in daily or []:
        try:
            if int(row.get("seconds") or 0) > 0:
                dates.add(datetime.fromisoformat(row["date"]).date())
        except Exception:
            continue
    return dates


def _calculate_current_streak_from_dates(activity_dates, reference_date):
    streak = 0
    cursor = reference_date
    while cursor in activity_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _normalize_abs_author(metadata):
    authors = (metadata or {}).get("authors") or []
    names = [author.get("name") for author in authors if isinstance(author, dict) and author.get("name")]
    return ", ".join(names)


def _recent_daily_from_mapping(days_map, tz, days=7):
    end_date = datetime.now(tz).date()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    days_map = days_map if isinstance(days_map, dict) else {}
    buckets = {
        str(key): int(round(float(value or 0)))
        for key, value in days_map.items()
    }
    return [
        {
            "date": day.isoformat(),
            "seconds": buckets.get(day.isoformat(), 0),
        }
        for day in _date_series(start_date, end_date)
    ]


def _heatmap_from_mapping(days_map, year):
    days_map = days_map if isinstance(days_map, dict) else {}
    heatmap = []
    for key, value in days_map.items():
        if str(key).startswith(f"{year}-"):
            heatmap.append({
                "date": str(key),
                "seconds": int(round(float(value or 0))),
            })
    heatmap.sort(key=lambda row: row["date"])
    return heatmap


def _summarize_activity(daily, total_seconds, total_days, reference_date):
    daily = daily or []
    week_total = sum(int(row.get("seconds") or 0) for row in daily)
    best_day = max(daily, key=lambda row: int(row.get("seconds") or 0), default=None)
    activity_dates = _activity_dates_from_daily(daily)
    return {
        "totalSeconds": int(total_seconds or 0),
        "totalDays": int(total_days or 0),
        "weekTotalSeconds": week_total,
        "dailyAverageSeconds": int(week_total / max(len(daily), 1)),
        "bestDay": best_day,
        "currentStreakDays": _calculate_current_streak_from_dates(activity_dates, reference_date),
    }


def _normalize_listening_session(session_data):
    metadata = session_data.get("mediaMetadata") or {}
    started_at = int((session_data.get("startedAt") or 0) / 1000)
    ended_at = int((session_data.get("updatedAt") or 0) / 1000) or started_at
    return {
        "id": session_data.get("id"),
        "activityType": "listening",
        "absId": session_data.get("libraryItemId"),
        "title": session_data.get("displayTitle") or metadata.get("title") or "Unknown title",
        "subtitle": metadata.get("subtitle"),
        "author": session_data.get("displayAuthor") or _normalize_abs_author(metadata),
        "durationSeconds": int(round(float(session_data.get("timeListening") or 0))),
        "startedAt": started_at,
        "endedAt": ended_at,
        "coverPath": session_data.get("coverPath"),
    }


def _years_from_days(all_days):
    years = set()
    for key in (all_days or {}):
        try:
            years.add(int(str(key).split("-")[0]))
        except (IndexError, ValueError):
            pass
    return sorted(years)


def _build_listening_yearly_recap(all_days, year, items_finished):
    """Year-in-review for listening: monthly hours from the ABS daily map.

    Per-month 'finished' is not available at daily granularity (would need an extra
    ABS media-progress call), so finishedBooks stays empty and booksFinished reflects
    the all-time items count for context only.
    """
    months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
    total_seconds = 0
    for key, value in (all_days or {}).items():
        date_str = str(key)
        if not date_str.startswith(f"{year}-"):
            continue
        try:
            month_index = int(date_str.split("-")[1]) - 1
        except (IndexError, ValueError):
            continue
        if month_index not in range(12):
            continue
        seconds = int(round(float(value or 0)))
        months[month_index]["seconds"] += seconds
        total_seconds += seconds
    return {
        "year": year,
        "months": months,
        "totalSeconds": total_seconds,
        "totalPages": 0,
        "booksFinished": int(items_finished or 0),
        "finishedBooks": [],
        "availableYears": _years_from_days(all_days),
    }


def _build_listening_book_list():
    """Per-audiobook rollup from the bridge's own AUDIOBOOK reading sessions."""
    stats_by_id = database_service.get_all_reading_stats()
    if not stats_by_id:
        return []
    books = {book.abs_id: book for book in database_service.get_all_books()}
    result = []
    for abs_id, stats in stats_by_id.items():
        listen_seconds = int(stats.get("listen_seconds") or 0)
        if listen_seconds <= 0:
            continue
        book = books.get(abs_id)
        result.append({
            "bookKey": f"abs:{abs_id}",
            "absId": abs_id,
            "isLinked": True,
            "title": getattr(book, "abs_title", None) or "Unknown book",
            "author": None,
            "totalSeconds": listen_seconds,
            "sessionCount": int(stats.get("session_count") or 0),
            "avgSessionSeconds": int(stats.get("avg_session_seconds") or 0),
            "lastReadAt": int(stats["last_session_time"]) if stats.get("last_session_time") else None,
        })
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _build_listening_stats_payload(tz):
    try:
        abs_client = container.abs_client()
    except Exception:
        return None

    raw_stats = abs_client.get_listening_stats()
    if not raw_stats:
        return None

    all_days = raw_stats.get("days") or {}
    daily = _recent_daily_from_mapping(all_days, tz, days=7)
    heatmap = _heatmap_from_mapping(all_days, datetime.now(tz).year)
    all_activity_dates = _activity_dates_from_daily([
        {"date": key, "seconds": value}
        for key, value in all_days.items()
    ])

    recent_sessions = raw_stats.get("recentSessions")
    if not isinstance(recent_sessions, list) or not recent_sessions:
        recent_sessions = abs_client.get_listening_sessions(limit=10)
    normalized_sessions = [
        _normalize_listening_session(session_data)
        for session_data in (recent_sessions or [])[:10]
        if isinstance(session_data, dict)
    ]

    summary = _summarize_activity(
        daily=daily,
        total_seconds=int(round(float(raw_stats.get("totalTime") or 0))),
        total_days=len(all_activity_dates),
        reference_date=datetime.now(tz).date(),
    )
    summary["itemsFinished"] = len(raw_stats.get("items") or {})
    summary["daysListened"] = len(all_activity_dates)

    session_durations = [int(s.get("durationSeconds") or 0) for s in normalized_sessions if s.get("durationSeconds")]
    summary["avgSessionSeconds"] = int(sum(session_durations) / len(session_durations)) if session_durations else 0
    summary["hoursPerDay"] = round(summary.get("dailyAverageSeconds", 0) / 3600, 2)

    return {
        "available": True,
        "stats": summary,
        "daily": daily,
        "heatmap": heatmap,
        "recentSessions": normalized_sessions,
        "activityDates": [day.isoformat() for day in sorted(all_activity_dates)],
        "trackedBookIds": sorted({
            session.get("absId") for session in normalized_sessions if session.get("absId")
        }),
        "books": _build_listening_book_list(),
        "yearlyRecap": _build_listening_yearly_recap(all_days, datetime.now(tz).year, summary["itemsFinished"]),
    }


def _build_reading_stats_payload(tz):
    tz_name = getattr(tz, "key", str(tz))
    summary = database_service.get_koreader_dashboard_summary(tz_name)
    daily = database_service.get_koreader_daily_totals(7, tz_name)
    heatmap = database_service.get_koreader_heatmap(datetime.now(tz).year, tz_name)
    recent_sessions = database_service.get_koreader_recent_sessions(10, tz_name)
    activity_dates = database_service.get_koreader_activity_dates(tz_name)
    hour_histogram = database_service.get_koreader_hour_histogram(tz_name)
    books = database_service.get_koreader_book_list(tz_name)
    yearly_recap = database_service.get_koreader_yearly_recap(datetime.now(tz).year, tz_name)

    if not summary and not any(int(row.get("seconds") or 0) > 0 for row in daily):
        return {
            "available": False,
            "stats": None,
            "daily": daily,
            "heatmap": heatmap,
            "recentSessions": [],
            "activityDates": [],
            "trackedBookIds": [],
            "trackedBookKeys": [],
            "hourHistogram": hour_histogram,
            "books": [],
            "yearlyRecap": yearly_recap,
        }

    stats = summary or {}
    stats.setdefault("booksTracked", 0)
    stats.setdefault("linkedBooksTracked", 0)
    stats.setdefault("unlinkedBooksTracked", 0)
    stats.setdefault("daysRead", len(activity_dates))
    stats.setdefault("totalSeconds", 0)
    stats.setdefault("pagesRead", 0)
    stats.setdefault("weekTotalSeconds", sum(int(row.get("seconds") or 0) for row in daily))
    stats.setdefault("dailyAverageSeconds", int(stats["weekTotalSeconds"] / max(len(daily), 1)))
    stats.setdefault("bestDay", max(daily, key=lambda row: int(row.get("seconds") or 0), default=None))
    stats.setdefault("currentStreakDays", _calculate_current_streak_from_dates(
        {datetime.fromisoformat(day).date() for day in activity_dates},
        datetime.now(tz).date(),
    ))
    stats.setdefault("trackedBookIds", [])
    stats.setdefault("trackedBookKeys", [])

    return {
        "available": True,
        "stats": stats,
        "daily": daily,
        "heatmap": heatmap,
        "recentSessions": recent_sessions,
        "activityDates": activity_dates,
        "trackedBookIds": stats.get("trackedBookIds") or [],
        "trackedBookKeys": stats.get("trackedBookKeys") or [],
        "hourHistogram": hour_histogram,
        "books": books,
        "yearlyRecap": yearly_recap,
    }


def _merge_daily_activity(listening_daily, reading_daily, tz):
    end_date = datetime.now(tz).date()
    start_date = end_date - timedelta(days=6)
    listening_map = {row["date"]: int(row.get("seconds") or 0) for row in listening_daily or []}
    reading_map = {row["date"]: int(row.get("seconds") or 0) for row in reading_daily or []}

    merged = []
    for day in _date_series(start_date, end_date):
        key = day.isoformat()
        listening_seconds = listening_map.get(key, 0)
        reading_seconds = reading_map.get(key, 0)
        merged.append({
            "date": key,
            "seconds": listening_seconds + reading_seconds,
            "listeningSeconds": listening_seconds,
            "readingSeconds": reading_seconds,
        })
    return merged


def _merge_heatmap_activity(listening_heatmap, reading_heatmap):
    merged = defaultdict(lambda: {"seconds": 0, "listeningSeconds": 0, "readingSeconds": 0})
    for row in listening_heatmap or []:
        key = row["date"]
        value = int(row.get("seconds") or 0)
        merged[key]["seconds"] += value
        merged[key]["listeningSeconds"] += value
    for row in reading_heatmap or []:
        key = row["date"]
        value = int(row.get("seconds") or 0)
        merged[key]["seconds"] += value
        merged[key]["readingSeconds"] += value
    return [
        {"date": key, **values}
        for key, values in sorted(merged.items())
    ]


def _merge_recent_sessions(listening_sessions, reading_sessions, limit=10):
    merged = list(listening_sessions or []) + list(reading_sessions or [])
    merged.sort(key=lambda row: int(row.get("endedAt") or 0), reverse=True)
    return merged[: max(int(limit or 10), 1)]


def _merge_book_lists(reading_books, listening_books):
    """Union reading + listening per-book rows by bookKey (linked books merge)."""
    merged = {}
    for source, items in (("reading", reading_books), ("listening", listening_books)):
        for item in items or []:
            key = item.get("bookKey")
            if not key:
                continue
            entry = merged.setdefault(key, {
                "bookKey": key, "absId": item.get("absId"),
                "isLinked": bool(item.get("isLinked")),
                "title": item.get("title"), "author": item.get("author"),
                "readingSeconds": 0, "listeningSeconds": 0, "totalSeconds": 0,
                "pagesRead": 0, "lastReadAt": 0, "percentComplete": None,
            })
            seconds = int(item.get("totalSeconds") or 0)
            if source == "reading":
                entry["readingSeconds"] += seconds
                entry["pagesRead"] = item.get("pagesRead") or entry["pagesRead"]
                if item.get("percentComplete") is not None:
                    entry["percentComplete"] = item.get("percentComplete")
            else:
                entry["listeningSeconds"] += seconds
            entry["totalSeconds"] = entry["readingSeconds"] + entry["listeningSeconds"]
            entry["lastReadAt"] = max(int(entry["lastReadAt"] or 0), int(item.get("lastReadAt") or 0))
            if not entry.get("title") and item.get("title"):
                entry["title"] = item.get("title")

    result = list(merged.values())
    for entry in result:
        entry["lastReadAt"] = entry["lastReadAt"] or None
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _build_combined_yearly_recap(reading_recap, listening_recap):
    """Merge reading + listening monthly hours; finished timeline comes from reading."""
    reading_months = (reading_recap or {}).get("months") or []
    listening_months = (listening_recap or {}).get("months") or []
    months = []
    for index in range(12):
        rm = reading_months[index] if index < len(reading_months) else {}
        lm = listening_months[index] if index < len(listening_months) else {}
        read_secs = int(rm.get("seconds") or 0)
        listen_secs = int(lm.get("seconds") or 0)
        months.append({
            "month": index + 1,
            "seconds": read_secs + listen_secs,
            "readingSeconds": read_secs,
            "listeningSeconds": listen_secs,
            "pages": int(rm.get("pages") or 0),
            "finished": int(rm.get("finished") or 0) + int(lm.get("finished") or 0),
        })
    finished_books = list((reading_recap or {}).get("finishedBooks") or [])
    available_years = sorted(
        set((reading_recap or {}).get("availableYears") or [])
        | set((listening_recap or {}).get("availableYears") or [])
    )
    return {
        "year": (reading_recap or listening_recap or {}).get("year"),
        "months": months,
        "totalSeconds": sum(month["seconds"] for month in months),
        "totalPages": (reading_recap or {}).get("totalPages") or 0,
        "booksFinished": len(finished_books),
        "finishedBooks": finished_books,
        "availableYears": available_years,
    }


def _build_combined_stats_payload(listening, reading, tz):
    listening_daily = (listening or {}).get("daily") or []
    reading_daily = (reading or {}).get("daily") or []
    combined_daily = _merge_daily_activity(listening_daily, reading_daily, tz)
    combined_heatmap = _merge_heatmap_activity(
        (listening or {}).get("heatmap"),
        (reading or {}).get("heatmap"),
    )
    combined_sessions = _merge_recent_sessions(
        (listening or {}).get("recentSessions"),
        (reading or {}).get("recentSessions"),
        limit=10,
    )

    listening_dates = {
        datetime.fromisoformat(day).date()
        for day in ((listening or {}).get("activityDates") or [])
    }
    reading_dates = {
        datetime.fromisoformat(day).date()
        for day in ((reading or {}).get("activityDates") or [])
    }
    all_activity_dates = listening_dates | reading_dates

    listening_book_keys = {
        f"abs:{book_id}"
        for book_id in ((listening or {}).get("trackedBookIds") or [])
        if book_id
    }
    reading_book_keys = set((reading or {}).get("trackedBookKeys") or [])

    combined_stats = {
        "activeDays": len(all_activity_dates),
        "totalSeconds": int(((listening or {}).get("stats") or {}).get("totalSeconds") or 0)
        + int(((reading or {}).get("stats") or {}).get("totalSeconds") or 0),
        "booksWithActivity": len(
            listening_book_keys | reading_book_keys
        ),
        "weekTotalSeconds": sum(int(row.get("seconds") or 0) for row in combined_daily),
        "dailyAverageSeconds": int(
            sum(int(row.get("seconds") or 0) for row in combined_daily) / max(len(combined_daily), 1)
        ),
        "bestDay": max(combined_daily, key=lambda row: int(row.get("seconds") or 0), default=None),
        "currentStreakDays": _calculate_current_streak_from_dates(all_activity_dates, datetime.now(tz).date()),
    }

    return {
        "available": bool((listening and listening.get("available")) or (reading and reading.get("available"))),
        "stats": combined_stats if combined_stats["totalSeconds"] or combined_stats["activeDays"] else None,
        "daily": combined_daily,
        "heatmap": combined_heatmap,
        "recentSessions": combined_sessions,
        "hourHistogram": (reading or {}).get("hourHistogram") or [],
        "books": _merge_book_lists((reading or {}).get("books"), (listening or {}).get("books")),
        "yearlyRecap": _build_combined_yearly_recap(
            (reading or {}).get("yearlyRecap"),
            (listening or {}).get("yearlyRecap"),
        ),
    }


def stats_view():
    return render_template('stats.html')


def api_stats():
    cached = _read_cached_stats()
    if cached is not None:
        return jsonify(cached)

    tz = _get_stats_timezone()
    listening = None
    reading = None

    try:
        listening = _build_listening_stats_payload(tz)
    except Exception as e:
        logger.warning("Stats API: listening stats build failed: %s", e, exc_info=True)
        listening = None

    try:
        reading = _build_reading_stats_payload(tz)
    except Exception as e:
        logger.warning("Stats API: reading stats build failed: %s", e, exc_info=True)
        reading = {
            "available": False,
            "stats": None,
            "daily": [],
            "heatmap": [],
            "recentSessions": [],
            "activityDates": [],
            "trackedBookIds": [],
            "trackedBookKeys": [],
        }

    combined = _build_combined_stats_payload(
        listening or {"available": False, "stats": None, "daily": [], "heatmap": [], "recentSessions": []},
        reading,
        tz,
    )

    response = {
        "listening": listening,
        "reading": {
            "available": reading.get("available"),
            "stats": reading.get("stats"),
            "daily": reading.get("daily"),
            "heatmap": reading.get("heatmap"),
            "recentSessions": reading.get("recentSessions"),
            "trackedBookIds": reading.get("trackedBookIds"),
            "trackedBookKeys": reading.get("trackedBookKeys"),
            "hourHistogram": reading.get("hourHistogram") or [],
            "books": reading.get("books") or [],
            "yearlyRecap": reading.get("yearlyRecap"),
        } if reading else {
            "available": False,
            "stats": None,
            "daily": [],
            "heatmap": [],
            "recentSessions": [],
            "trackedBookIds": [],
            "trackedBookKeys": [],
            "hourHistogram": [],
            "books": [],
            "yearlyRecap": None,
        },
        "combined": combined,
    }
    _write_cached_stats(response)
    return jsonify(response)


def api_stats_reading_day():
    date_str = str(request.args.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "Missing date"}), 400

    try:
        target_date = datetime.fromisoformat(date_str).date()
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    try:
        tz = _get_stats_timezone()
        payload = database_service.get_koreader_books_for_date(
            target_date.isoformat(),
            getattr(tz, "key", str(tz)),
        )
    except Exception as e:
        logger.warning("Stats API: reading day drilldown failed for %s: %s", date_str, e, exc_info=True)
        return jsonify({"error": "Failed to load reading day details"}), 500

    return jsonify(payload)


def api_stats_reading_calendar():
    month_str = str(request.args.get("month") or "").strip()
    if not month_str:
        return jsonify({"error": "Missing month"}), 400

    try:
        datetime.fromisoformat(f"{month_str}-01")
    except ValueError:
        return jsonify({"error": "Invalid month format"}), 400

    try:
        tz = _get_stats_timezone()
        payload = database_service.get_koreader_calendar_month(
            month_str,
            getattr(tz, "key", str(tz)),
        )
    except Exception as e:
        logger.warning("Stats API: reading calendar failed for %s: %s", month_str, e, exc_info=True)
        return jsonify({"error": "Failed to load reading calendar"}), 500

    return jsonify(payload)


def api_stats_book_detail():
    key = str(request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "Missing key"}), 400

    try:
        tz = _get_stats_timezone()
        tz_name = getattr(tz, "key", str(tz))
        reading = database_service.get_koreader_book_detail(key, tz_name)

        abs_id = None
        if key.startswith("abs:"):
            abs_id = key.split("abs:", 1)[1]
        elif reading and reading.get("absId"):
            abs_id = reading.get("absId")

        listening = None
        if abs_id:
            row = database_service.get_reading_stats(abs_id)
            if row and int(row.get("listen_seconds") or 0) > 0:
                book = database_service.get_book(abs_id)
                listening = {
                    "absId": abs_id,
                    "title": getattr(book, "abs_title", None),
                    "totalSeconds": int(row.get("listen_seconds") or 0),
                    "sessionCount": int(row.get("session_count") or 0),
                    "avgSessionSeconds": int(row.get("avg_session_seconds") or 0),
                    "lastReadAt": int(row["last_session_time"]) if row.get("last_session_time") else None,
                }

        if not reading and not listening:
            return jsonify({"error": "No detail for this book"}), 404

        title = (reading or {}).get("title") or (listening or {}).get("title") or "Unknown book"
        return jsonify({
            "bookKey": key, "absId": abs_id, "title": title,
            "reading": reading, "listening": listening,
        })
    except Exception as e:
        logger.warning("Stats API: book detail failed for %s: %s", key, e, exc_info=True)
        return jsonify({"error": "Failed to load book detail"}), 500


def api_stats_yearly_recap():
    scope = str(request.args.get("scope") or "combined").strip().lower()
    year_str = str(request.args.get("year") or "").strip()

    tz = _get_stats_timezone()
    tz_name = getattr(tz, "key", str(tz))
    try:
        year = int(year_str) if year_str else datetime.now(tz).year
    except ValueError:
        return jsonify({"error": "Invalid year"}), 400

    try:
        reading_recap = database_service.get_koreader_yearly_recap(year, tz_name)

        listening_recap = None
        try:
            abs_client = container.abs_client()
            raw_stats = abs_client.get_listening_stats() if abs_client else None
        except Exception:
            raw_stats = None
        if raw_stats:
            listening_recap = _build_listening_yearly_recap(
                raw_stats.get("days") or {}, year, len(raw_stats.get("items") or {})
            )

        if scope == "reading":
            return jsonify(reading_recap)
        if scope == "listening":
            return jsonify(listening_recap or _build_listening_yearly_recap({}, year, 0))
        return jsonify(_build_combined_yearly_recap(reading_recap, listening_recap))
    except Exception as e:
        logger.warning("Stats API: yearly recap failed for %s/%s: %s", scope, year, e, exc_info=True)
        return jsonify({"error": "Failed to load yearly recap"}), 500


def api_status():
    """Return status of all books from database service"""
    user = current_user()
    user_id = user.id if user else None
    books = database_service.get_all_books(user_id=user_id)
    all_states = database_service.get_all_states(
        user_id=user_id
    )
    books = _dashboard_visible_books_for_user(books, user)
    all_hardcover = database_service.get_all_hardcover_details()
    all_storygraph = database_service.get_all_storygraph_details()
    all_reading_stats = database_service.get_all_reading_stats(user_id=user_id)
    cached_booklore_by_filename = _index_cached_booklore_books(database_service.get_all_booklore_books())
    integrations = _build_dashboard_integrations()
    mappings, _ = _build_dashboard_mappings(
        books,
        all_states,
        integrations,
        all_hardcover=all_hardcover,
        all_storygraph=all_storygraph,
        reading_stats_by_book=all_reading_stats,
        cached_booklore_by_filename=cached_booklore_by_filename,
    )

    return jsonify({"mappings": mappings})


def _build_dashboard_progress_rows(books, all_states):
    """The per-book fields the dashboard's periodic refresh actually redraws.

    Derived from Book/State rows and scalar alignment status: no display-metadata
    resolution, no per-book service lookups, and above all no alignment map —
    which the full dashboard build loads per book to compute the drift badge
    (issue #412)."""
    states_by_book = _group_dashboard_states_by_book(all_states)
    ctc_aligned_book_ids = database_service.get_ctc_aligned_book_ids()
    rows = []

    for book in books or []:
        abs_id = getattr(book, "abs_id", None)
        if not abs_id:
            continue

        states = {}
        latest_update_time = 0
        max_progress = 0.0
        for state in states_by_book.get(abs_id, []):
            if state.last_updated and state.last_updated > latest_update_time:
                latest_update_time = state.last_updated
            pct_val = round(state.percentage * 100, 1) if state.percentage is not None else 0
            states[state.client_name] = {
                "timestamp": state.timestamp or 0,
                "percentage": pct_val,
            }
            if state.percentage is not None:
                max_progress = max(max_progress, pct_val)

        rows.append({
            "abs_id": abs_id,
            "ctc_aligned": abs_id in ctc_aligned_book_ids,
            "unified_progress": min(max_progress, 100.0),
            "last_sync": _format_dashboard_last_sync(latest_update_time),
            "last_sync_unix": latest_update_time,
            "states": states,
        })

    return rows


def api_status_progress():
    """Compact position feed for the dashboard's periodic refresh.

    The refresh only redraws progress numbers and the 'last synced' line, but
    /api/status rebuilds the whole dashboard payload for every visible book —
    including the per-book alignment lookups behind the drift badge, which the
    refresh never reads. Serving those few fields from the database alone stops a
    dashboard left open from pegging a core every 30 seconds (issue #412)."""
    user = current_user()
    user_id = user.id if user else None
    books = database_service.get_all_books(user_id=user_id)
    all_states = database_service.get_all_states(user_id=user_id)
    books = _dashboard_visible_books_for_user(books, user)

    return jsonify({"mappings": _build_dashboard_progress_rows(books, all_states)})


def logs_view():
    """Display logs frontend with filtering capabilities.

    `?embed=1` renders without the page chrome so the viewer can be hosted
    inside an iframe (the Settings → Logs tab)."""
    embed = request.args.get('embed') in ('1', 'true', 'yes')
    return render_template('logs.html', embed=embed)


def _tail_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return list(deque(f, maxlen=max_lines))
    except OSError:
        return []


def api_logs():
    """API endpoint for fetching logs with filtering and pagination."""
    try:
        # Get query parameters
        lines_count = request.args.get('lines', 1000, type=int)
        min_level = request.args.get('level', 'DEBUG')
        search_term = request.args.get('search', '').lower()
        offset = request.args.get('offset', 0, type=int)

        # Limit lines count for performance
        lines_count = min(lines_count, 5000)

        # Read log files (current and backups)
        all_lines = []

        # Read current log file
        if LOG_PATH and LOG_PATH.exists():
            all_lines.extend(_tail_text_lines(LOG_PATH, lines_count))

        # Read backup files if needed (for more history)
        if LOG_PATH and lines_count > len(all_lines):
            for i in range(1, 6):  # Check up to 5 backup files
                backup_path = Path(str(LOG_PATH) + f'.{i}')
                if backup_path.exists():
                    remaining = lines_count - len(all_lines)
                    backup_lines = _tail_text_lines(backup_path, remaining)
                    all_lines = backup_lines + all_lines
                    if len(all_lines) >= lines_count:
                        break

        # Parse and filter logs
        log_levels = {
            'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50
        }
        min_level_num = log_levels.get(min_level.upper(), 10)

        parsed_logs = []
        for line in all_lines:
            line = line.strip()
            if not line:
                continue

            # Parse log line format: [2024-01-09 10:30:45] LEVEL - MODULE: MESSAGE
            try:
                if line.startswith('[') and '] ' in line:
                    timestamp_end = line.find('] ')
                    timestamp_str = line[1:timestamp_end]
                    rest = line[timestamp_end + 2:]

                    if ': ' in rest:
                        level_module_str, message = rest.split(': ', 1)

                        # Check if format includes module (LEVEL - MODULE)
                        if ' - ' in level_module_str:
                            level_str, module_str = level_module_str.split(' - ', 1)
                        else:
                            # Old format without module
                            level_str = level_module_str
                            module_str = 'unknown'

                        level_num = log_levels.get(level_str.upper(), 20)

                        # Apply filters
                        if level_num >= min_level_num:
                            if not search_term or search_term in message.lower() or search_term in level_str.lower() or search_term in module_str.lower():
                                parsed_logs.append({
                                    'timestamp': timestamp_str,
                                    'level': level_str,
                                    'message': message,
                                    'module': module_str,
                                    'raw': line
                                })
                    else:
                        # Line without level, treat as INFO
                        if min_level_num <= 20:
                            if not search_term or search_term in rest.lower():
                                parsed_logs.append({
                                    'timestamp': timestamp_str,
                                    'level': 'INFO',
                                    'message': rest,
                                    'module': 'unknown',
                                    'raw': line
                                })
                else:
                    # Raw line without timestamp, treat as INFO
                    if min_level_num <= 20:
                        if not search_term or search_term in line.lower():
                            parsed_logs.append({
                                'timestamp': '',
                                'level': 'INFO',
                                'message': line,
                                'module': 'unknown',
                                'raw': line
                            })
            except Exception:
                # If parsing fails, include as raw line
                if not search_term or search_term in line.lower():
                    parsed_logs.append({
                        'timestamp': '',
                        'level': 'INFO',
                        'message': line,
                        'module': 'unknown',
                        'raw': line
                    })

        # Get recent logs first, then apply pagination
        recent_logs = parsed_logs[-lines_count:] if len(parsed_logs) > lines_count else parsed_logs

        # Apply offset for pagination
        if offset > 0:
            recent_logs = recent_logs[:-offset] if offset < len(recent_logs) else []

        return jsonify({
            'logs': recent_logs,
            'total_lines': len(parsed_logs),
            'displayed_lines': len(recent_logs),
            'has_more': len(parsed_logs) > lines_count + offset
        })

    except Exception as e:
        logger.error(f"❌ Error fetching logs: {e}", exc_info=True)
        return jsonify({'error': 'Failed to fetch logs', 'logs': [], 'total_lines': 0, 'displayed_lines': 0}), 500


def api_logs_live():
    """API endpoint for fetching recent live logs from memory."""
    try:
        # Get query parameters
        count = request.args.get('count', 50, type=int)
        min_level = request.args.get('level', 'DEBUG')
        search_term = request.args.get('search', '').lower()

        # Limit count for performance
        count = min(count, 500)

        log_levels = {
            'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50
        }
        min_level_num = log_levels.get(min_level.upper(), 10)

        # Get recent logs from memory
        recent_logs = memory_log_handler.get_recent_logs(count * 2)  # Get more to filter

        # Filter logs
        filtered_logs = []
        for log_entry in recent_logs:
            level_num = log_levels.get(log_entry['level'], 20)

            # Apply filters
            if level_num >= min_level_num:
                if not search_term or search_term in log_entry['message'].lower() or search_term in log_entry['level'].lower():
                    filtered_logs.append(log_entry)

        # Return most recent filtered logs
        result_logs = filtered_logs[-count:] if len(filtered_logs) > count else filtered_logs

        return jsonify({
            'logs': result_logs,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error(f"❌ Error fetching live logs: {e}", exc_info=True)
        return jsonify({'error': 'Failed to fetch live logs', 'logs': [], 'timestamp': datetime.now().isoformat()}), 500


def view_log():
    """Legacy endpoint - redirect to new logs page."""
    return redirect(url_for('logs_view'))


# ---------------- SUGGESTION API ROUTES ----------------
def get_suggestions():
    suggestions = database_service.get_all_pending_suggestions()
    result = []
    for s in suggestions:
        try:
            matches = json.loads(s.matches_json) if s.matches_json else []
        except Exception:
            matches = []

        result.append({
            "id": s.id,
            "source_id": s.source_id,
            "title": s.title,
            "author": s.author,
            "cover_url": s.cover_url,
            "matches": matches,
            "created_at": s.created_at.isoformat()
        })
    return jsonify(result)


def dismiss_suggestion(source_id):
    if database_service.dismiss_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


def ignore_suggestion(source_id):
    if database_service.ignore_suggestion(source_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Not found"}), 404


def clear_stale_suggestions():
    count = database_service.clear_stale_suggestions()
    logger.info(f"🧹 Cleared {count} stale suggestions from database")
    return jsonify({"success": True, "count": count})


def clean_inactive_cache():
    """Delete audio_cache, transcript dirs, and cached EPUBs for books that are not active."""
    active_books = database_service.get_books_by_status('active')
    active_ids = {b.abs_id for b in active_books}
    active_ebook_files = {b.ebook_filename for b in active_books if b.ebook_filename}
    active_orig_files = {b.original_ebook_filename for b in active_books if b.original_ebook_filename}
    protected_files = active_ebook_files | active_orig_files

    deleted_audio = 0
    deleted_transcripts = 0
    deleted_epubs = 0

    audio_cache_root = DATA_DIR / "audio_cache"
    if audio_cache_root.exists():
        for entry in audio_cache_root.iterdir():
            if entry.is_dir() and entry.name not in active_ids:
                try:
                    shutil.rmtree(entry)
                    deleted_audio += 1
                    logger.info(f"Cleaned audio cache: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean audio cache {entry.name}: {e}", exc_info=True)

    transcript_root = DATA_DIR / "transcripts" / "storyteller"
    if transcript_root.exists():
        for entry in transcript_root.iterdir():
            if entry.is_dir() and entry.name not in active_ids:
                try:
                    shutil.rmtree(entry)
                    deleted_transcripts += 1
                    logger.info(f"Cleaned transcript dir: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean transcript dir {entry.name}: {e}", exc_info=True)

    try:
        epub_cache_dir = Path(container.epub_cache_dir())
    except Exception:
        epub_cache_dir = DATA_DIR / "epub_cache"
    if epub_cache_dir.exists():
        for entry in epub_cache_dir.iterdir():
            if entry.is_file() and entry.name not in protected_files:
                try:
                    entry.unlink()
                    deleted_epubs += 1
                    logger.info(f"Cleaned cached epub: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean cached epub {entry.name}: {e}", exc_info=True)

    logger.info(f"Cache cleanup complete: {deleted_audio} audio, {deleted_transcripts} transcript, {deleted_epubs} epub(s) removed")
    return jsonify({"success": True, "deleted_audio": deleted_audio, "deleted_transcripts": deleted_transcripts, "deleted_epubs": deleted_epubs})


def _run_storyteller_backfill():
    """
    Bulk backfill storyteller transcripts for currently matched storyteller books.
    """
    assets_dir_raw = os.environ.get("STORYTELLER_ASSETS_DIR", "").strip()
    if not assets_dir_raw:
        return {
            "success": False,
            "error": "STORYTELLER_ASSETS_DIR is not configured",
            "scanned": 0,
            "ingested": 0,
            "missing": 0,
            "failed": 0,
            "aligned": 0,
            "duration_seconds": 0.0,
        }, 400

    started_at = time.time()
    books = database_service.get_all_books() if database_service else []
    storyteller_books = [
        b for b in books
        if getattr(b, "storyteller_uuid", None) or getattr(b, "transcript_source", None) == "storyteller"
    ]

    summary = {
        "success": True,
        "scanned": 0,
        "ingested": 0,
        "missing": 0,
        "failed": 0,
        "aligned": 0,
        "duration_seconds": 0.0,
    }

    abs_client = container.abs_client() if container else None
    alignment_service = getattr(manager, "alignment_service", None) if manager else None
    ebook_parser = container.ebook_parser() if container else None

    for book in storyteller_books:
        summary["scanned"] += 1
        abs_id = book.abs_id
        try:
            item_details = abs_client.get_item_details(abs_id) if abs_client else None
            chapters = item_details.get("media", {}).get("chapters", []) if item_details else []
            manifest_path = ingest_storyteller_transcripts(abs_id, book.abs_title or "", chapters)
            if not manifest_path:
                summary["missing"] += 1
                continue

            book.transcript_source = "storyteller"
            book.transcript_file = manifest_path
            summary["ingested"] += 1

            aligned = False
            if alignment_service:
                storyteller_transcript = StorytellerTranscript(manifest_path)
                book_text = ""
                if ebook_parser and book.ebook_filename:
                    try:
                        epub_filename = book.original_ebook_filename or book.ebook_filename
                        epub_path = safe_cache_path(container.epub_cache_dir(), epub_filename)
                        if epub_path and epub_path.exists():
                            book_text, _ = ebook_parser.extract_text_and_map(epub_path)
                    except Exception as e:
                        logger.warning(f"Could not extract text for storyteller backfill: {e}", exc_info=True)
                        
                aligned = alignment_service.align_storyteller_and_store(abs_id, storyteller_transcript, ebook_text=book_text)
                if aligned:
                    summary["aligned"] += 1

            if aligned:
                book.transcript_file = "DB_MANAGED"
                if getattr(book, "status", None) in (None, "", "pending", "processing", "failed_retry_later", "failed_permanent", "crashed"):
                    book.status = "active"
            else:
                book.status = "pending"

            database_service.save_book(book)
        except Exception as e:
            summary["failed"] += 1
            logger.warning(f"Storyteller backfill failed for '{abs_id}': {e}", exc_info=True)

    summary["duration_seconds"] = round(time.time() - started_at, 3)
    logger.info(
        "Storyteller backfill summary: "
        f"scanned={summary['scanned']} ingested={summary['ingested']} "
        f"aligned={summary['aligned']} missing={summary['missing']} failed={summary['failed']} "
        f"duration={summary['duration_seconds']}s"
    )
    return summary, 200


def api_storyteller_backfill():
    summary, status_code = _run_storyteller_backfill()
    return jsonify(summary), status_code


def _extract_series_from_title(title: str) -> tuple:
    """Return (series_name, series_sequence) parsed out of a numbered title."""
    return _series_from_title(title)


def _series_seq_equal(stored: "float | int | str | None", resolved: "float | None") -> bool:
    """True when a stored series sequence already matches a freshly resolved one."""
    if stored is None and resolved is None:
        return True
    if stored is None or resolved is None:
        return False
    try:
        return abs(float(stored) - float(resolved)) < 1e-9
    except (TypeError, ValueError):
        return False


def _series_refresh_action(stored_name: "str | None", stored_seq: "float | int | str | None",
                           resolution) -> str:
    """Decide what a resolved series means for a stored row.

    Returns ``update``, ``unchanged``, ``clear``, ``keep`` or ``none``.
    ``clear`` is only ever returned when a service that owns the book actually
    answered — silence from an offline or unconfigured library must leave the
    stored series alone rather than read as "the series was deleted".
    """
    had_series = bool((stored_name or "").strip())
    if resolution.name:
        if had_series and resolution.name == stored_name:
            if _series_seq_equal(stored_seq, resolution.sequence):
                return "unchanged"
            # Never trade a known volume number for an unknown one. Some
            # libraries carry the series name but no index, and dropping the
            # stored number would sort that book to the end of its own series.
            if resolution.sequence is None and stored_seq is not None:
                return "unchanged"
        return "update"
    if had_series:
        return "clear" if resolution.service_answered else "keep"
    return "none"


def _series_backfill_clients() -> dict:
    """Return the clients the series backfill resolves against, keyed by kwarg name."""
    return {
        "abs_client": container.abs_client(),
        "bookorbit_client": container.bookorbit_client(),
        "booklore_client": container.booklore_client(),
        "kavita_client": container.kavita_client(),
        "ebook_parser": container.ebook_parser(),
    }


@admin_required
def api_audio_repoint_plan():
    """Work out which ABS-audio mappings could move to BookOrbit. Changes nothing.

    Slow by nature: confirming a candidate costs a BookOrbit detail call, and that
    duration check is what proves the book's existing alignment still describes the
    audio. Detail responses are cached for an hour, so a re-run is cheap.
    """
    try:
        plan = container.audio_repoint_service().build_plan()
    except Exception as e:
        logger.error(f"❌ Audio repoint plan failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, **plan})


@admin_required
def api_audio_repoint_apply():
    """Apply a set of repoint selections: [{'abs_id': ..., 'target_id': ...}]."""
    payload = request.get_json(silent=True) or {}
    selections = payload.get("selections") or []
    if not isinstance(selections, list):
        return jsonify({"success": False, "error": "selections must be a list"}), 400
    try:
        result = container.audio_repoint_service().apply(selections)
    except Exception as e:
        logger.error(f"❌ Audio repoint apply failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, **result})


@admin_required
def api_audio_repoint_undo():
    """Send repointed books back to Audiobookshelf.

    With no body, reverts every book that was repointed (rows whose audio is
    BookOrbit but whose key is still an ABS item id).
    """
    payload = request.get_json(silent=True) or {}
    abs_ids = payload.get("abs_ids")
    if abs_ids is not None and not isinstance(abs_ids, list):
        return jsonify({"success": False, "error": "abs_ids must be a list"}), 400
    try:
        result = container.audio_repoint_service().undo(abs_ids)
    except Exception as e:
        logger.error(f"❌ Audio repoint undo failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, **result})


@admin_required
def api_series_backfill():
    """Backfill series_name/series_sequence, optionally re-resolving known rows.

    Default (fill) mode only touches books with no series. Refresh mode
    (``{"refresh": true}``) re-resolves every book and clears a series the
    owning service no longer reports — but only when that service actually
    answered, so an outage or an unconfigured client never wipes good data.

    Resolves each row against the service that owns its metadata — ABS for ABS
    audio, the ebook library (BookOrbit/Grimmory/Kavita) for ebook-only rows —
    and falls back to parsing the number out of the title. Writes via direct SQL
    UPDATE to avoid ORM session lifecycle issues.
    """
    import time as _time
    from types import SimpleNamespace
    import sqlalchemy as _sa
    start = _time.time()
    db = container.database_service()

    payload = request.get_json(silent=True) or {}
    refresh = bool(payload.get("refresh")) or (
        str(request.args.get("refresh", "")).strip().lower() in ("1", "true", "on", "yes")
    )

    clients = _series_backfill_clients()

    def _usable(client) -> bool:
        try:
            return client is not None and client.is_configured()
        except Exception:
            return False

    if not any(_usable(c) for c in clients.values()):
        return jsonify({"error": "No library service is configured"}), 400

    columns = (
        "SELECT abs_id, abs_title, audio_source, audio_source_id, "
        "ebook_source, ebook_source_id, ebook_filename, series_name, series_sequence FROM books"
    )
    query = columns if refresh else (
        columns + " WHERE series_name IS NULL OR series_name = ''"
    )

    # Collect all rows that need updating — read-only pass
    with db.get_session() as session:
        rows = session.execute(_sa.text(query)).fetchall()

    updates = []   # list of (abs_id, series_name, series_sequence)
    clears = []    # abs_ids whose owning service says the series is gone
    unresolved = 0
    unchanged = 0
    kept_no_answer = 0
    failed = 0

    for (abs_id, abs_title, audio_source, audio_source_id,
         ebook_source, ebook_source_id, ebook_filename, stored_name, stored_seq) in rows:
        book_row = SimpleNamespace(
            abs_id=abs_id,
            abs_title=abs_title,
            audio_source=audio_source,
            audio_source_id=audio_source_id,
            ebook_source=ebook_source,
            ebook_source_id=ebook_source_id,
            ebook_filename=ebook_filename,
        )
        try:
            resolution = resolve_series_details(book_row, force_refresh=refresh, **clients)
        except Exception as e:
            logger.warning(
                f"Series backfill lookup failed for '{sanitize_log_data(abs_title)}': {e}",
                exc_info=True,
            )
            failed += 1
            continue

        action = _series_refresh_action(stored_name, stored_seq, resolution)
        if action == "update":
            updates.append((abs_id, resolution.name, resolution.sequence))
            logger.debug(
                f"Series backfill queued: '{resolution.name}' #{resolution.sequence} "
                f"→ '{sanitize_log_data(abs_title)}' (via {resolution.source})"
            )
        elif action == "unchanged":
            unchanged += 1
        elif action == "clear":
            clears.append(abs_id)
            logger.info(
                f"📚 Series cleared for '{sanitize_log_data(abs_title)}': source no longer "
                f"reports a series (was '{sanitize_log_data(stored_name)}')"
            )
        elif action == "keep":
            kept_no_answer += 1
        else:
            unresolved += 1

    # Write pass — single transaction, plain SQL
    if updates or clears:
        with db.get_session() as session:
            for abs_id, sname, sseq in updates:
                session.execute(
                    _sa.text("UPDATE books SET series_name = :sname, series_sequence = :sseq WHERE abs_id = :abs_id"),
                    {"sname": sname, "sseq": sseq, "abs_id": abs_id},
                )
            for abs_id in clears:
                session.execute(
                    _sa.text("UPDATE books SET series_name = NULL, series_sequence = NULL WHERE abs_id = :abs_id"),
                    {"abs_id": abs_id},
                )

    duration = round(_time.time() - start, 1)
    logger.info(
        f"Series backfill complete (mode={'refresh' if refresh else 'fill'}): "
        f"updated={len(updates)} cleared={len(clears)} unchanged={unchanged} "
        f"unresolved={unresolved} kept_no_answer={kept_no_answer} failed={failed} "
        f"duration={duration}s"
    )
    return jsonify({
        "mode": "refresh" if refresh else "fill",
        "scanned": len(rows),
        "updated": len(updates),
        "cleared": len(clears),
        "unchanged": unchanged,
        "unresolved": unresolved,
        "kept_no_answer": kept_no_answer,
        "failed": failed,
        "duration_seconds": duration,
        "sample_updates": [{"abs_id": a, "series": s, "seq": q} for a, s, q in updates[:10]],
    }), 200


@admin_required
def api_debug_abs_series():
    """Return the raw series metadata ABS sends for a given abs_id. For debugging only."""
    abs_id = request.args.get("abs_id", "").strip()
    if not abs_id:
        return jsonify({"error": "abs_id query param required"}), 400
    abs_client = container.abs_client()
    if not abs_client or not abs_client.is_configured():
        return jsonify({"error": "ABS not configured"}), 400

    abs_client._update_session_headers()
    url = f"{abs_client.base_url}/api/items/{abs_id}"
    try:
        r = abs_client.session.get(url, timeout=abs_client.timeout)
        if r.status_code != 200:
            return jsonify({
                "error": f"ABS returned HTTP {r.status_code}",
                "url_called": url,
                "response_preview": r.text[:500],
            }), 502
        item = r.json()
    except Exception as e:
        return jsonify({"error": str(e), "url_called": url}), 502

    meta = item.get("media", {}).get("metadata", {}) or {}
    sname, sseq = _extract_series_from_abs_metadata(meta)
    return jsonify({
        "abs_id": abs_id,
        "media_metadata_keys": list(meta.keys()),
        "series_field": meta.get("series"),
        "seriesName_field": meta.get("seriesName"),
        "parsed_series_name": sname,
        "parsed_series_sequence": sseq,
    })


def proxy_cover(abs_id):
    """Proxy cover access to allow loading covers from local network ABS instances."""
    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)
    try:
        abs_client = uc().abs_client
        if not abs_client or not abs_client.is_configured():
            # No per-user ABS credentials: serve from the admin/global library.
            abs_client = container.abs_client()
        token = abs_client.token
        base_url = abs_client.base_url
        if not token or not base_url:
            return "ABS not configured", 500

        url = f"{base_url.rstrip('/')}/api/items/{abs_id}/cover?token={token}"

        # Stream the response to avoid loading large images into memory
        req = requests.get(url, stream=True, timeout=10)
        if req.status_code == 200:
            from flask import Response
            response = Response(
                req.iter_content(chunk_size=1024),
                content_type=req.headers.get('content-type', 'image/jpeg'),
            )
            # This proxy is live, but with no cache headers browsers apply their own
            # heuristic freshness and a cover replaced upstream can look stale for a
            # long time. A short max-age plus the upstream validator keeps it cheap
            # and lets a changed cover appear quickly.
            response.headers['Cache-Control'] = 'public, max-age=300'
            upstream_etag = req.headers.get('ETag')
            if upstream_etag:
                response.headers['ETag'] = upstream_etag
            return response
        else:
            return "Cover not found", 404
    except Exception as e:
        logger.error(f"❌ Error proxying cover for '{abs_id}': {e}", exc_info=True)
        return "Error loading cover", 500


# --- Logger setup (already present) ---
logger = logging.getLogger(__name__)

def get_booklore_libraries():
    """Return available Grimmory libraries."""
    if not container.booklore_client().is_configured():
        return jsonify({"error": "Grimmory not configured"}), 400

    libraries = container.booklore_client().get_libraries()
    return jsonify(libraries)


def get_booklore_shelves():
    """Return available Grimmory shelves (regular and magic)."""
    if not container.booklore_client().is_configured():
        return jsonify({"error": "Grimmory not configured"}), 400

    try:
        shelves = container.booklore_client().get_all_shelves()
        magic_shelves = container.booklore_client().get_all_magic_shelves()
        
        all_shelves = shelves + magic_shelves
        result = []
        
        for s in all_shelves:
            is_magic = s.get("magicShelf") or s.get("magic") or s.get("isMagic", False)
            name = s.get("name", "Unknown")
            
            # Add emoji prefix for UI distinction
            if is_magic and not name.startswith("🪄"):
                name = f"🪄 {name}"
                
            result.append({
                "id": s.get("name"),  # Use original name as ID
                "name": name,
                "count": s.get("bookCount", 0)
            })
            
        # Sort alphabetically by the original name
        result.sort(key=lambda x: x["id"].lower() if x["id"] else "")
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Error fetching Grimmory shelves: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


def get_abs_libraries():
    """Return available Audiobookshelf libraries."""
    if not container.abs_client().is_configured():
        return jsonify({"error": "Audiobookshelf not configured"}), 400

    libraries = container.abs_client().get_libraries()
    return jsonify(libraries)


def proxy_booklore_audiobook_cover(book_id):
    """Stream a Grimmory audiobook cover through the backend."""
    user = current_user()
    # The route id identifies a Grimmory book, which can be paired with an ebook
    # from a different provider (or a different book id on the same provider),
    # or be the text source of an ebook-only mapping.
    book = (
        (
            database_service.get_book_by_audio_source('BookLore', str(book_id))
            or database_service.get_book_by_ebook_source('BookLore', str(book_id))
        )
        if database_service else None
    )
    if book and book.abs_id:
        if not _user_may_modify_book(user, book.abs_id):
            return _forbidden_book_response(json_response=True)
    elif user is not None and not getattr(user, 'is_admin', False):
        # Non-admin requesting a cover for an unknown Grimmory book — forbid
        return _forbidden_book_response(json_response=True)

    client = container.booklore_client()
    if not client.is_configured():
        return "Grimmory not configured", 400

    try:
        content, content_type = client.get_audiobook_cover_bytes(book_id)
        if not content:
            return "Cover not found", 404
        from flask import Response

        return Response(content, content_type=content_type or "image/jpeg")
    except Exception as e:
        logger.error(f"❌ Error proxying Grimmory audiobook cover for '{book_id}': {e}", exc_info=True)
        return "Error loading cover", 500


def proxy_bookorbit_audiobook_cover(book_id):
    """Stream a BookOrbit book cover through the backend."""
    user = current_user()
    # The route id identifies a BookOrbit book, which may be reached as an
    # audiobook or as an ebook-only mapping's text source.
    book = (
        (
            database_service.get_book_by_audio_source('BookOrbit', str(book_id))
            or database_service.get_book_by_ebook_source('BookOrbit', str(book_id))
        )
        if database_service else None
    )
    if book and book.abs_id:
        if not _user_may_modify_book(user, book.abs_id):
            return _forbidden_book_response(json_response=True)
    elif user is not None and not getattr(user, 'is_admin', False):
        # Non-admin requesting a cover for an unknown BookOrbit book — forbid
        return _forbidden_book_response(json_response=True)

    client = uc().bookorbit_client
    if not client or not client.is_configured():
        return "BookOrbit not configured", 400

    try:
        content, content_type = client.get_cover_bytes(book_id)
        if not content:
            return "Cover not found", 404
        from flask import Response

        return Response(content, content_type=content_type or "image/jpeg")
    except Exception as e:
        logger.error(f"❌ Error proxying BookOrbit cover for '{book_id}': {e}", exc_info=True)
        return "Error loading cover", 500


def proxy_kavita_cover(series_id):
    """Stream a Kavita series cover without exposing the user's auth key."""
    client = uc().kavita_client
    if not client or not client.is_configured():
        return "Kavita not configured", 400
    try:
        content, content_type = client.get_cover_bytes(series_id)
        if not content:
            return "Cover not found", 404
        from flask import Response

        response = Response(content, content_type=content_type or "image/jpeg")
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response
    except Exception as e:
        logger.error("Error proxying Kavita cover for '%s': %s", series_id, e, exc_info=True)
        return "Error loading cover", 500


def api_booklore_refresh():
    """Clear Grimmory cache and trigger a full refresh."""
    client = container.booklore_client()
    if not client.is_configured():
        return jsonify({"success": False, "error": "Grimmory not configured"}), 400

    try:
        refreshed = client.clear_and_refresh()
    except Exception as e:
        logger.error(f"❌ Grimmory cache refresh failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

    if not refreshed:
        return jsonify({"success": False, "error": "Grimmory refresh failed"}), 500

    reconcile = getattr(client, "reconcile_mapping_filename_drift", None)
    if callable(reconcile):
        reconcile()

    return jsonify({"success": True, "message": "Grimmory cache refreshed successfully"})


def _test_conn_error(e: Exception) -> str:
    """Extract a user-friendly message from a requests exception."""
    msg = str(e)
    if isinstance(e, requests.exceptions.ConnectionError):
        inner = str(e.args[0]) if e.args else msg
        if 'NameResolutionError' in inner or 'getaddrinfo' in inner or 'Name or service not known' in inner:
            return "DNS lookup failed — check the hostname"
        if 'Connection refused' in inner or 'No connection could be made' in inner:
            return "Connection refused — is the server running?"
        return "Cannot reach server — check the URL"
    if isinstance(e, requests.exceptions.Timeout):
        return "Connection timed out — server may be down"
    if isinstance(e, requests.exceptions.MissingSchema):
        return "Invalid URL — missing http:// or https://"
    return msg


def _coerce_test_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _coerce_test_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_test_url(value: str) -> str:
    url = _coerce_test_str(value).rstrip('/')
    if url and not url.lower().startswith(('http://', 'https://')):
        url = f"http://{url}"
    return url


def _normalize_abs_test_url(value: str) -> str:
    url = _coerce_test_str(value)
    if is_abs_disabled_value(url):
        return ABS_DISABLED_SENTINEL
    return _normalize_test_url(url)


def _build_test_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _is_builtin_kosync_test_url(url: str) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return False

    valid_ports = {5757}
    configured_port = (os.environ.get("KOSYNC_PORT") or "").strip()
    if configured_port:
        try:
            valid_ports.add(int(configured_port))
        except ValueError:
            logger.warning(f"Invalid KOSYNC_PORT '{configured_port}' while testing KOSync settings", exc_info=True)

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    return port in valid_ports


def _run_test_connection(service: str, payload: dict):
    testers = {
        'abs': lambda data: _test_abs(
            _normalize_abs_test_url(data.get('ABS_SERVER')),
            _coerce_test_str(data.get('ABS_KEY')),
        ),
        'kosync': lambda data: _test_kosync(
            _coerce_test_bool(data.get('KOSYNC_ENABLED')),
            _normalize_test_url(data.get('KOSYNC_SERVER')),
            _coerce_test_str(data.get('KOSYNC_USER')),
            _coerce_test_str(data.get('KOSYNC_KEY')),
            _coerce_test_str(data.get('KOSYNC_AUTH_METHOD')) or 'kosync',
        ),
        'storyteller': lambda data: _test_storyteller(
            _coerce_test_bool(data.get('STORYTELLER_ENABLED')),
            _normalize_test_url(data.get('STORYTELLER_API_URL')),
            _coerce_test_str(data.get('STORYTELLER_USER')),
            _coerce_test_str(data.get('STORYTELLER_PASSWORD')),
        ),
        'booklore': lambda data: _test_booklore(
            _coerce_test_bool(data.get('BOOKLORE_ENABLED')),
            _normalize_test_url(data.get('BOOKLORE_SERVER')),
            _coerce_test_str(data.get('BOOKLORE_USER')),
            _coerce_test_str(data.get('BOOKLORE_PASSWORD')),
        ),
        'bookorbit': lambda data: _test_bookorbit(
            _coerce_test_bool(data.get('BOOKORBIT_ENABLED')),
            _normalize_test_url(data.get('BOOKORBIT_SERVER')),
            _coerce_test_str(data.get('BOOKORBIT_USER')),
            _coerce_test_str(data.get('BOOKORBIT_PASSWORD')),
        ),
        'kavita': lambda data: _test_kavita(
            _coerce_test_bool(data.get('KAVITA_ENABLED')),
            _normalize_test_url(data.get('KAVITA_SERVER')),
            _coerce_test_str(data.get('KAVITA_API_KEY')),
        ),
        'bookfusion': lambda data: _test_bookfusion(
            _coerce_test_bool(data.get('BOOKFUSION_ENABLED')),
            _normalize_test_url(data.get('BOOKFUSION_API_URL')),
            _coerce_test_str(data.get('BOOKFUSION_ACCESS_TOKEN')),
        ),
        'cwa': lambda data: _test_cwa(
            _coerce_test_bool(data.get('CWA_ENABLED')),
            _normalize_test_url(data.get('CWA_SERVER')),
            _coerce_test_str(data.get('CWA_USERNAME')),
            _coerce_test_str(data.get('CWA_PASSWORD')),
            _coerce_test_str(data.get('CWA_SYNC_TOKEN')),
        ),
        'readest': lambda data: _test_readest(
            _coerce_test_str(data.get('READEST_EMAIL')),
            _coerce_test_str(data.get('READEST_PASSWORD')),
            _normalize_test_url(data.get('READEST_SUPABASE_URL')),
        ),
        'hardcover': lambda data: _test_hardcover(
            _coerce_test_bool(data.get('HARDCOVER_ENABLED')),
            _coerce_test_str(data.get('HARDCOVER_TOKEN')),
        ),
        'storygraph': lambda data: _test_storygraph(
            _coerce_test_bool(data.get('STORYGRAPH_ENABLED')),
            _coerce_test_str(data.get('STORYGRAPH_SESSION_COOKIE')),
            _coerce_test_str(data.get('STORYGRAPH_REMEMBER_USER_TOKEN')),
        ),
        'telegram': lambda data: _test_telegram(
            _coerce_test_bool(data.get('TELEGRAM_ENABLED')),
            _coerce_test_str(data.get('TELEGRAM_BOT_TOKEN')),
        ),
        'ollama': lambda data: _test_llm_provider(
            _coerce_test_bool(data.get('OLLAMA_ENABLED')),
            _coerce_test_str(data.get('LLM_PROVIDER')) or 'ollama',
            _normalize_test_url(data.get('LLM_BASE_URL')),
            _coerce_test_str(data.get('LLM_API_KEY')),
            _coerce_test_str(data.get('LLM_EMBED_MODEL')),
            _coerce_test_str(data.get('LLM_CHAT_MODEL')),
            _normalize_test_url(data.get('OLLAMA_URL')),
            _coerce_test_str(data.get('OLLAMA_EMBED_MODEL')),
            _coerce_test_str(data.get('OLLAMA_CHAT_MODEL')),
        ),
        'whispercpp': lambda data: _test_whispercpp(
            _normalize_test_url(data.get('WHISPER_CPP_URL')),
        ),
    }
    tester = testers.get(service)
    if not tester:
        return jsonify({"ok": False, "message": f"Unknown service: {service}"}), 400
    try:
        return jsonify(tester(payload))
    except Exception as e:
        return jsonify({"ok": False, "message": _test_conn_error(e)})


@admin_required
def test_connection(service: str):
    """Test connectivity with diagnostic error messages."""
    return _run_test_connection(service, request.get_json(silent=True) or {})


def api_bookfusion_device_start() -> object:
    """Start the BookFusion device-link flow for the current user."""
    client = uc().bookfusion_client
    data = client.start_device_link()
    if not data:
        return jsonify({"ok": False, "message": "Could not start BookFusion device link"}), 502
    return jsonify({
        "ok": True,
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 600),
    })


def api_bookfusion_device_poll() -> object:
    """Poll BookFusion's device-link token endpoint for the current user."""
    payload = request.get_json(silent=True) or {}
    device_code = str(payload.get("device_code") or "").strip()
    if not device_code:
        return jsonify({"ok": False, "error": "missing_device_code"}), 400
    result = uc().bookfusion_client.poll_token(device_code)
    if result.get("ok"):
        user = current_user()
        if user is not None:
            try:
                container.user_client_registry().invalidate(user.id)
            except Exception as e:
                logger.debug("Could not invalidate BookFusion client bundle after link for user %s: %s", user.id, e)
    status = 200 if result.get("ok") or result.get("error") in ("authorization_pending", "slow_down") else 400
    return jsonify(result), status


def _bookfusion_client_for_user(user_id):
    """Build a BookFusion client bound to a specific user so a device-link token
    persists to THAT user's credentials (used by the admin link-on-behalf flow)."""
    from src.api.bookfusion_client import BookFusionClient
    creds = database_service.get_user_credentials(user_id) or {}
    return BookFusionClient(credentials=creds, database_service=database_service, user_id=user_id)


@admin_required
def admin_user_bookfusion_device_start(user_id) -> object:
    """Start the BookFusion device-link flow on behalf of a specific user."""
    target = database_service.get_user(user_id)
    if not target:
        return jsonify({"ok": False, "message": "User not found"}), 404
    data = _bookfusion_client_for_user(target.id).start_device_link()
    if not data:
        return jsonify({"ok": False, "message": "Could not start BookFusion device link"}), 502
    return jsonify({
        "ok": True,
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 600),
    })


@admin_required
def admin_user_bookfusion_device_poll(user_id) -> object:
    """Poll the BookFusion device-link token endpoint on behalf of a specific
    user, persisting the resulting access token to THAT user (not the admin)."""
    target = database_service.get_user(user_id)
    if not target:
        return jsonify({"ok": False, "error": "user_not_found"}), 404
    payload = request.get_json(silent=True) or {}
    device_code = str(payload.get("device_code") or "").strip()
    if not device_code:
        return jsonify({"ok": False, "error": "missing_device_code"}), 400
    result = _bookfusion_client_for_user(target.id).poll_token(device_code)
    if result.get("ok"):
        try:
            container.user_client_registry().invalidate(target.id)
        except Exception as e:
            logger.debug("Could not invalidate BookFusion client bundle after admin link for user %s: %s", target.id, e)
    status = 200 if result.get("ok") or result.get("error") in ("authorization_pending", "slow_down") else 400
    return jsonify(result), status


def _serialize_bookfusion_search_item(item: dict) -> dict:
    """Normalize one BookFusion library search row for dashboard linking."""
    book_id = item.get("id") or item.get("book_id")
    return {
        "id": str(book_id) if book_id not in (None, "") else "",
        "title": str(item.get("title") or item.get("name") or "").strip(),
        "author": _coerce_author_display(item.get("authors") or item.get("author")),
    }


def api_bookfusion_search() -> object:
    """Search the current user's BookFusion library for link targets."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"books": []})
    client = uc().bookfusion_client
    if not client.is_configured():
        return jsonify({"error": "BookFusion not configured"}), 400
    books = client.search_books(page=1, per_page=50, q=query) or []
    query_lower = query.lower()
    normalized = []
    for item in books:
        row = _serialize_bookfusion_search_item(item)
        if not row["id"]:
            continue
        haystack = f"{row['title']} {row['author']}".lower()
        if query_lower and query_lower not in haystack:
            continue
        normalized.append(row)
    return jsonify({"books": normalized})


def api_bookfusion_link(abs_id: str) -> object:
    """Link a shared BookBridge book to the current user's BookFusion book."""
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else database_service._default_user_id()
    payload = request.get_json(silent=True) or {}
    bookfusion_id = str(payload.get("bookfusion_id") or "").strip()
    if not bookfusion_id:
        return jsonify({"success": False, "error": "Missing BookFusion book id"}), 400
    reader_client = uc().bookfusion_client
    if not reader_client.is_configured():
        return jsonify({"success": False, "error": "BookFusion not configured"}), 400
    try:
        probe_url = reader_client.get_download_url(bookfusion_id)
    except Exception as exc:
        logger.warning(
            "BookFusion link probe failed for id %s: %s",
            bookfusion_id, exc, exc_info=True,
        )
        probe_url = None
    if not probe_url:
        return jsonify({
            "success": False,
            "error": (
                "This BookFusion result is not available to the reader API "
                "and cannot be linked yet. Upload the book to BookFusion first, "
                "then re-search and link it."
            ),
        }), 400
    link = database_service.set_user_bookfusion_link(
        user_id,
        abs_id,
        bookfusion_id,
        title=str(payload.get("title") or "").strip() or None,
        author=str(payload.get("author") or "").strip() or None,
    )
    return jsonify({
        "success": bool(link),
        "bookfusion_id": (link or {}).get("bookfusion_id"),
        "title": (link or {}).get("title"),
    })


def api_bookfusion_unlink(abs_id: str) -> object:
    """Remove the current user's BookFusion link for a BookBridge book."""
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else database_service._default_user_id()
    return jsonify({"success": database_service.delete_user_bookfusion_link(user_id, abs_id)})


def _cleanup_temp(path: Path) -> None:
    """Remove *path* if it exists, swallowing any errors."""
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.debug("Could not remove temp file %s: %s", path, exc)


def _readaloud_integrity_error(path: Path) -> str | None:
    """Return a user-facing error when an EPUB has broken audio overlays."""
    try:
        with zipfile.ZipFile(path) as epub:
            names = set(epub.namelist())
            audio_refs = 0
            missing: set[str] = set()
            for smil_name in (name for name in names if name.lower().endswith(".smil")):
                root = ElementTree.fromstring(epub.read(smil_name))
                for element in root.iter():
                    if element.tag.rsplit("}", 1)[-1].lower() != "audio":
                        continue
                    src = str(element.get("src") or "").strip()
                    if not src:
                        continue
                    audio_refs += 1
                    ref_path = unquote(urlparse(src).path)
                    if ref_path.startswith("/"):
                        target = posixpath.normpath(ref_path.lstrip("/"))
                    else:
                        target = posixpath.normpath(
                            posixpath.join(posixpath.dirname(smil_name), ref_path)
                        )
                    if target not in names:
                        missing.add(target)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        logger.warning("Could not validate Storyteller ReadAloud EPUB %s: %s", path, exc, exc_info=True)
        return "Storyteller returned an invalid ReadAloud EPUB. Wait for processing to finish and try again."

    if not audio_refs:
        return "Storyteller ReadAloud EPUB is incomplete (no narration audio was found). Wait for processing to finish and try again."
    if missing:
        count = len(missing)
        noun = "file is" if count == 1 else "files are"
        return f"Storyteller ReadAloud EPUB is incomplete ({count} narration audio {noun} missing). Wait for processing to finish and try again."
    return None


def api_bookfusion_upload(abs_id: str) -> object:
    """Upload an EPUB to BookFusion and link it.

    Accepts an optional JSON body ``{"variant": "standard" | "readaloud"}``.
    ``"standard"`` (default) uploads the book's local EPUB file. ``"readaloud"``
    fetches the full Storyteller ReadAloud EPUB3 with embedded narration audio
    and uploads that instead; requires the book to have a ``storyteller_uuid``.
    """
    book = database_service.get_book(abs_id)
    if not book:
        return jsonify({"success": False, "error": "Book not found"}), 404
    user = current_user()
    if not _user_may_modify_book(user, abs_id):
        return _forbidden_book_response(json_response=True)
    user_id = user.id if user is not None else database_service._default_user_id()

    # Parse variant from JSON body (default: standard)
    payload = request.get_json(silent=True) or {}
    variant = str(payload.get("variant") or "standard").strip().lower()

    # ------------------------------------------------------------------
    # ReadAloud variant: fetch full audio-intact EPUB from Storyteller
    # ------------------------------------------------------------------
    if variant == "readaloud":
        if not book.storyteller_uuid:
            return jsonify({"success": False, "error": "This book is not linked to Storyteller"}), 400

        tmp_dir = Path(os.environ.get("DATA_DIR", "/data")) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"bf_readaloud_{abs_id}_{uuid.uuid4().hex}.epub"

        storyteller_client = uc().storyteller_client
        if not storyteller_client.is_configured():
            return jsonify({"success": False, "error": "Storyteller is not configured for this user"}), 400

        try:
            ok = storyteller_client.download_book(book.storyteller_uuid, tmp_path, polling=False)
        except Exception as exc:
            logger.warning("Storyteller ReadAloud EPUB download failed for %s: %s", abs_id, exc, exc_info=True)
            ok = False
        if not ok:
            _cleanup_temp(tmp_path)
            return jsonify({"success": False, "error": "Could not download the Storyteller ReadAloud EPUB (it may not be processed yet)"}), 502

        integrity_error = _readaloud_integrity_error(tmp_path)
        if integrity_error:
            logger.warning("Rejected incomplete Storyteller ReadAloud EPUB for %s: %s", abs_id, integrity_error)
            _cleanup_temp(tmp_path)
            return jsonify({"success": False, "error": integrity_error}), 502

        upload_client = uc().bookfusion_upload_client
        if not upload_client.is_configured():
            _cleanup_temp(tmp_path)
            return jsonify({"success": False, "error": "BookFusion Calibre API key not set"}), 400

        metadata = extract_epub_metadata(str(tmp_path))
        try:
            result = upload_client.upload_epub(str(tmp_path), metadata, s3_timeout=_S3_TIMEOUT_LARGE)
        finally:
            _cleanup_temp(tmp_path)

    # ------------------------------------------------------------------
    # Standard variant: existing local-EPUB flow (unchanged behavior)
    # ------------------------------------------------------------------
    else:
        filename = book.original_ebook_filename or book.ebook_filename
        if not filename:
            return jsonify({"success": False, "error": "No local ebook to upload"}), 400
        try:
            epub_path = container.ebook_parser().resolve_book_path(filename)
        except Exception as exc:
            logger.warning("Could not resolve ebook path for %s: %s", filename, exc, exc_info=True)
            return jsonify({"success": False, "error": "Local ebook file not found"}), 400
        if not epub_path or not epub_path.exists():
            return jsonify({"success": False, "error": "Local ebook file not found"}), 400

        upload_client = uc().bookfusion_upload_client
        if not upload_client.is_configured():
            return jsonify({"success": False, "error": "BookFusion Calibre API key not set"}), 400

        metadata = extract_epub_metadata(str(epub_path))
        result = upload_client.upload_epub(str(epub_path), metadata)

    # ------------------------------------------------------------------
    # Shared result handling (both variants converge here)
    # ------------------------------------------------------------------
    if result.status == "created":
        book_id = result.book_id
        bookfusion_id_str = str(book_id) if book_id is not None else None
        link = database_service.set_user_bookfusion_link(
            user_id, abs_id, bookfusion_id_str,
            title=str(metadata.get("title") or "").strip() or None,
            author=str((metadata.get("authors") or [None])[0] or "").strip() or None,
        )
        if not link:
            logger.warning("BookFusion upload succeeded but link creation failed for %s / id=%s", abs_id, book_id)
        return jsonify({
            "success": True,
            "bookfusion_id": book_id,
            "created": True,
        })

    if result.status == "duplicate":
        title = metadata.get("title", "")
        author = (metadata.get("authors") or [None])[0] or ""
        search_id = _resolve_duplicate_bookfusion_id(upload_client, title, author)
        if search_id is not None:
            link = database_service.set_user_bookfusion_link(
                user_id, abs_id, str(search_id),
                title=str(metadata.get("title") or "").strip() or None,
                author=str((metadata.get("authors") or [None])[0] or "").strip() or None,
            )
            return jsonify({
                "success": True,
                "bookfusion_id": search_id,
                "created": False,
            })
        return jsonify({
            "success": False,
            "duplicate": True,
            "error": "Already in your BookFusion library",
        }), 409

    # error
    return jsonify({"success": False, "error": result.message}), 502


def _resolve_duplicate_bookfusion_id(upload_client, title: str, author: str = "") -> int | None:
    """Try to find a BookFusion book id for an already-uploaded duplicate.

    Called when upload init returns 422 (file digest already known). Uses the
    reader client (``uc().bookfusion_client``) to search by title and verifies
    that the returned book's title (and first author, when available) match
    the local metadata before linking.
    """
    if not title:
        return None
    reader_client = uc().bookfusion_client
    if not reader_client.is_configured():
        return None
    try:
        results = reader_client.search_books(page=1, per_page=5, q=title) or []
    except Exception as exc:
        logger.warning("BookFusion duplicate search failed: %s", exc, exc_info=True)
        return None
    title_lower = title.strip().lower()
    author_lower = author.strip().lower() if author else ""
    for item in results:
        item_id = item.get("id")
        if item_id is None:
            continue
        # Verify title matches (case-insensitive)
        item_title = (item.get("title") or "").strip().lower()
        if item_title != title_lower:
            continue
        # Verify first author matches if we have one
        if author_lower:
            item_authors = item.get("authors") or item.get("author") or ""
            first_author = ""
            if isinstance(item_authors, list):
                first_author = (item_authors[0] or "").strip().lower() if item_authors else ""
            elif isinstance(item_authors, str):
                first_author = item_authors.strip().lower()
            if first_author != author_lower:
                continue
        try:
            candidate_id = int(item_id)
        except (ValueError, TypeError):
            continue
        try:
            probe_url = reader_client.get_download_url(candidate_id)
        except Exception as exc:
            logger.debug(
                "BookFusion duplicate probe failed for id %s: %s",
                candidate_id, exc,
            )
            continue
        if not probe_url:
            logger.debug(
                "BookFusion duplicate id %s not accessible, skipping",
                candidate_id,
            )
            continue
        return candidate_id
    return None


def _test_llm_provider(
    enabled: bool,
    provider: str,
    base_url: str,
    api_key: str,
    embed_model: str,
    chat_model: str,
    ollama_url: str,
    ollama_embed_model: str,
    ollama_chat_model: str,
) -> dict:
    provider = (provider or "ollama").strip().lower()
    if provider in {"openai-compatible", "openai_compat", "llama", "llama-server", "llama_swap", "llama-swap"}:
        provider = "openai_compatible"
    if provider == "ollama":
        return _test_ollama(
            enabled,
            ollama_url,
            embed_model or ollama_embed_model,
            chat_model or ollama_chat_model,
        )
    if not enabled:
        return {"ok": False, "message": "LLM provider is disabled"}
    if provider == "openai":
        base_url = base_url or "https://api.openai.com/v1"
        api_key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return {"ok": False, "message": "Missing OpenAI API key"}
        embed_model = embed_model or "text-embedding-3-small"
        chat_model = chat_model or "gpt-4o-mini"
    elif provider == "openai_compatible":
        if not base_url:
            return {"ok": False, "message": "Missing OpenAI-compatible base URL"}
        if not embed_model or not chat_model:
            return {"ok": False, "message": "Missing embedding or chat model name"}
    else:
        return {"ok": False, "message": f"Unknown LLM provider: {provider}"}
    return _test_openai_compatible(provider, base_url, api_key, embed_model, chat_model)


def _test_openai_compatible(provider: str, base_url: str, api_key: str, embed_model: str, chat_model: str) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=10)
    if r.status_code != 200:
        label = "OpenAI" if provider == "openai" else "OpenAI-compatible endpoint"
        return {"ok": False, "message": f"{label} returned HTTP {r.status_code}"}
    models = [m.get("id", "") for m in (r.json() or {}).get("data", []) if isinstance(m, dict) and m.get("id")]
    missing = [m for m in (embed_model, chat_model) if models and m not in models]
    if missing:
        details = ", ".join(missing)
        return {
            "ok": False,
            "message": f"Connected, but configured model(s) were not listed by /models: {details}",
        }
    return {
        "ok": True,
        "message": f"Connected. {embed_model} embeddings ready, {chat_model} judge ready",
    }


def _test_whispercpp(url: str) -> dict:
    """Test whisper.cpp / OpenAI-compatible transcription server reachability."""
    if not url:
        return {"ok": False, "message": "Missing Whisper.cpp server URL"}

    try:
        resp = requests.get(url, timeout=5)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "message": _test_conn_error(e)}

    # The transcription endpoint typically only accepts POST, so any HTTP
    # response (including 404/405 on GET) proves the server is reachable.
    return {"ok": True, "message": f"Server reachable at {url} (HTTP {resp.status_code})"}


def _test_ollama(enabled: bool, url: str, embed_model: str, chat_model: str) -> dict:
    if not enabled:
        return {"ok": False, "message": "Ollama is disabled"}
    if not url:
        return {"ok": False, "message": "Missing Ollama server URL"}

    embed_model = embed_model or "nomic-embed-text"
    chat_model = chat_model or "qwen2.5:14b"

    r = requests.get(f"{url}/api/tags", timeout=10)
    if r.status_code != 200:
        return {"ok": False, "message": f"Ollama returned HTTP {r.status_code}"}

    models = [m.get("name", "") for m in (r.json() or {}).get("models", []) if m.get("name")]

    def _present(name: str) -> bool:
        # Ollama tags include the tag suffix (e.g. "qwen2.5:14b"); match exact or base name.
        base = name.split(":", 1)[0]
        return any(m == name or m.split(":", 1)[0] == base for m in models)

    # Label each model by the features it powers so the operator knows what breaks.
    roles = {
        embed_model: "embeddings — suggestion re-ranking & alignment fallback",
        chat_model: "judge — match disambiguation & tracker matching",
    }
    missing = [m for m in (embed_model, chat_model) if not _present(m)]
    if missing:
        details = "; ".join(f"{m} ({roles[m]})" for m in missing)
        pulls = " && ".join(f"ollama pull {m}" for m in missing)
        return {
            "ok": False,
            "message": (
                f"Connected, but missing model(s): {details}. "
                f"Those features will silently fall back until you run: {pulls}"
            ),
        }
    embed_info = _ollama_show_info(url, embed_model)
    chat_info = _ollama_show_info(url, chat_model)

    def _annotate(name: str, info: dict) -> str:
        parts = []
        if info.get("context_length"):
            parts.append(f"ctx {info['context_length']}")
        if info.get("capabilities"):
            parts.append(", ".join(info["capabilities"]))
        return f"{name} ✓ ({'; '.join(parts)})" if parts else f"{name} ✓"

    message = f"Connected. {_annotate(embed_model, embed_info)}, {_annotate(chat_model, chat_info)}"
    embed_caps = embed_info.get("capabilities") or []
    if embed_caps and "embedding" not in embed_caps:
        message += f". Warning: {embed_model} does not report embedding capability"
    return {"ok": True, "message": message}


def _ollama_show_info(url: str, model: str) -> dict:
    """Best-effort /api/show probe: {'context_length': int|None, 'capabilities': list}."""
    info = {"context_length": None, "capabilities": []}
    try:
        r = requests.post(f"{url}/api/show", json={"model": model}, timeout=10)
        if r.status_code != 200:
            return info
        data = r.json() or {}
        model_info = data.get("model_info") or {}
        for key, value in model_info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                info["context_length"] = value
                break
        caps = data.get("capabilities")
        if isinstance(caps, list):
            info["capabilities"] = [c for c in caps if isinstance(c, str)]
    except Exception:
        pass
    return info


def _test_abs(url: str, token: str) -> dict:
    if is_abs_disabled_value(url) or is_abs_disabled_value(token):
        return {"ok": False, "message": "Audiobookshelf is intentionally disabled"}
    if not url or not token:
        return {"ok": False, "message": "Missing server URL or API token"}
    r = requests.get(f"{url}/api/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code == 200:
        username = r.json().get('username', 'unknown')
        return {"ok": True, "message": f"Connected as '{username}'"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": f"Authentication failed ({r.status_code}) — check your API token"}
    return {"ok": False, "message": f"Server returned {r.status_code}"}


def _test_kosync(
    enabled: bool,
    url: str,
    user: str,
    key: str,
    auth_method: str = 'kosync',
) -> dict:
    if not enabled or not url:
        return {"ok": False, "message": "KOSync not configured or disabled"}
    if not user or not key:
        return {"ok": False, "message": "Missing username or password"}

    request_kwargs = kosync_request_kwargs(user, key, auth_method)
    healthcheck_status = None
    healthcheck_error = None
    try:
        healthcheck = requests.get(
            _build_test_url(url, "healthcheck"),
            timeout=5,
            **request_kwargs,
        )
        healthcheck_status = healthcheck.status_code
    except Exception as e:
        healthcheck_error = str(e)

    if _is_builtin_kosync_test_url(url):
        if healthcheck_status == 200:
            return {
                "ok": True,
                "message": (
                    "Built-in KOSync bridge is reachable. "
                    "Typed credentials look ready and will take effect after you save settings."
                ),
            }
        if healthcheck_status is not None:
            return {
                "ok": False,
                "message": f"Built-in KOSync bridge healthcheck returned {healthcheck_status}",
            }
        return {
            "ok": False,
            "message": (
                "Built-in KOSync bridge is not reachable"
                + (f": {healthcheck_error}" if healthcheck_error else "")
            ),
        }

    auth = requests.get(
        _build_test_url(url, "users/auth"),
        timeout=5,
        **request_kwargs,
    )
    if auth.status_code == 200:
        if healthcheck_status not in (None, 200):
            return {
                "ok": True,
                "message": (
                    "Server is reachable and credentials are valid "
                    f"(healthcheck returned {healthcheck_status})"
                ),
            }
        if healthcheck_error:
            return {
                "ok": True,
                "message": (
                    "Server is reachable and credentials are valid "
                    f"(healthcheck error: {healthcheck_error})"
                ),
            }
        return {"ok": True, "message": "Server is reachable and credentials are valid"}
    if auth.status_code in (401, 403):
        return {"ok": False, "message": f"Authentication failed ({auth.status_code}) — check username or password"}
    if auth.status_code == 500:
        return {"ok": False, "message": "Remote KOSync server is not configured"}
    if healthcheck_status is not None:
        return {
            "ok": False,
            "message": (
                f"Auth check returned {auth.status_code}; "
                f"healthcheck returned {healthcheck_status}"
            ),
        }
    if healthcheck_error:
        return {
            "ok": False,
            "message": (
                f"Auth check returned {auth.status_code}; "
                f"healthcheck error: {healthcheck_error}"
            ),
        }
    return {"ok": False, "message": f"Auth check returned {auth.status_code}"}


def _test_storyteller(enabled: bool, url: str, user: str, pwd: str) -> dict:
    if not enabled:
        return {"ok": False, "message": "Storyteller is disabled"}
    if not url or not user or not pwd:
        return {"ok": False, "message": "Missing URL, username, or password"}
    responses = []
    for endpoint, payload in (
        ("/api/v2/token", {"usernameOrEmail": user, "password": pwd}),
        ("/api/token", {"username": user, "password": pwd}),
    ):
        r = requests.post(
            f"{url}{endpoint}",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        responses.append(r.status_code)
        if r.status_code == 200:
            return {"ok": True, "message": "Authenticated successfully"}
        if r.status_code in (401, 403):
            return {"ok": False, "message": "Invalid username or password"}
        if r.status_code not in (400, 404, 405, 422):
            break
    if any(status == 422 for status in responses):
        return {"ok": False, "message": "Invalid username or password"}
    return {"ok": False, "message": f"Login returned {responses[-1]}"}


def _test_booklore(enabled: bool, url: str, user: str, pwd: str) -> dict:
    if not enabled:
        return {"ok": False, "message": "Grimmory is disabled"}
    if not url or not user or not pwd:
        return {"ok": False, "message": "Missing URL, username, or password"}
    r = requests.post(
        f"{url}/api/v1/auth/login",
        json={"username": user, "password": pwd},
        timeout=10,
    )
    if r.status_code == 200:
        return {"ok": True, "message": "Authenticated successfully"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Invalid username or password"}
    return {"ok": False, "message": f"Login returned {r.status_code}"}


def _test_bookorbit(enabled: bool, url: str, user: str, pwd: str) -> dict:
    if not enabled:
        return {"ok": False, "message": "BookOrbit is disabled"}
    if not url or not user or not pwd:
        return {"ok": False, "message": "Missing URL, username, or password"}
    r = requests.post(
        f"{url}/api/v1/auth/login",
        json={"username": user, "password": pwd},
        timeout=10,
    )
    if r.status_code == 200:
        return {"ok": True, "message": "Authenticated successfully"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Invalid username or password"}
    if r.status_code == 429:
        return {"ok": False, "message": "Login throttled (429) — wait a minute and try again"}
    return {"ok": False, "message": f"Login returned {r.status_code}"}


def _test_kavita(enabled: bool, url: str, api_key: str) -> dict:
    if not enabled:
        return {"ok": False, "message": "Kavita is disabled"}
    if not url or not api_key:
        return {"ok": False, "message": "Missing URL or authentication key"}
    response = requests.get(
        f"{url}/api/Library/libraries",
        headers={"x-api-key": api_key, "X-Kavita-Client": "BookBridge"},
        timeout=10,
    )
    if response.status_code == 200:
        return {"ok": True, "message": "Authenticated successfully"}
    if response.status_code in (401, 403):
        return {"ok": False, "message": "Invalid authentication key"}
    return {"ok": False, "message": f"Kavita returned {response.status_code}"}


def _test_cwa(enabled: bool, url: str, user: str, pwd: str, sync_token: str = "") -> dict:
    if not enabled or not url:
        return {"ok": False, "message": "CWA not configured or disabled"}

    results = []

    # Test OPDS if credentials are provided
    if user and pwd:
        try:
            r = requests.get(f"{url}/opds", auth=(user, pwd), timeout=5)
            if r.status_code == 200 and not r.text.lstrip().lower().startswith(('<!doctype html', '<html')):
                results.append("OPDS: OK")
            elif r.status_code in (401, 403):
                results.append("OPDS: Invalid credentials")
            else:
                results.append(f"OPDS: Failed ({r.status_code})")
        except Exception as e:
            results.append(f"OPDS: {_test_conn_error(e)}")

    # Test Kobo sync if token is provided
    if sync_token:
        try:
            r = requests.get(f"{url}/kobo/{sync_token}/v1/initialization", timeout=5)
            if r.status_code == 200:
                results.append("Sync: OK")
            elif r.status_code in (401, 403):
                results.append("Sync: Invalid token")
            else:
                results.append(f"Sync: Failed ({r.status_code})")
        except Exception as e:
            results.append(f"Sync: {_test_conn_error(e)}")

    if not results:
        return {"ok": False, "message": "No OPDS credentials or sync token configured"}

    all_ok = all("OK" in r for r in results)
    return {"ok": all_ok, "message": "\n".join(results)}


def _test_hardcover(enabled: bool, token: str) -> dict:
    token = token.strip()
    if not enabled:
        return {"ok": False, "message": "Hardcover is disabled"}
    if not token:
        return {"ok": False, "message": "Missing API token"}
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    r = requests.post(
        "https://api.hardcover.app/v1/graphql",
        json={"query": "{ me { id username } }"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=10,
    )
    if r.status_code == 200:
        data = r.json()
        me = data.get('data', {}).get('me')
        if isinstance(me, list):
            me = me[0] if me and isinstance(me[0], dict) else None
        elif not isinstance(me, dict):
            me = None
        if me:
            username = me.get('username', 'unknown')
            return {"ok": True, "message": f"Connected as '{username}'"}
        errors = data.get('errors', [])
        if errors:
            return {"ok": False, "message": f"API error: {errors[0].get('message', 'unknown')}"}
        return {"ok": False, "message": "Invalid API token — no user data returned"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "Invalid API token"}
    return {"ok": False, "message": f"API returned {r.status_code}"}


def _test_readest(email: str, password: str, supabase_url: str) -> dict:
    """Validate Readest credentials by logging in — without persisting tokens."""
    from src.api.readest_client import ReadestClient
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "message": "Enter both email and password"}
    creds = {"READEST_EMAIL": email, "READEST_PASSWORD": password}
    if supabase_url:
        creds["READEST_SUPABASE_URL"] = supabase_url
    client = ReadestClient(credentials=creds)  # no db/user_id → no persistence
    if client.login(email, password, persist=False):
        return {"ok": True, "message": f"Signed in to Readest as {email}"}
    return {"ok": False, "message": "Login rejected — check email/password (Google sign-in accounts have no password)"}


def _test_bookfusion(enabled: bool, api_url: str, access_token: str) -> dict:
    """Validate a BookFusion access token without persisting anything."""
    if not enabled:
        return {"ok": False, "message": "BookFusion is disabled"}
    if not access_token:
        return {"ok": False, "message": "Link BookFusion first"}
    from src.api.bookfusion_client import BookFusionClient
    creds = {
        "BOOKFUSION_ENABLED": "true",
        "BOOKFUSION_ACCESS_TOKEN": access_token,
    }
    if api_url:
        creds["BOOKFUSION_API_URL"] = api_url
    client = BookFusionClient(credentials=creds)
    if client.check_connection():
        return {"ok": True, "message": "Connected to BookFusion"}
    return {"ok": False, "message": "BookFusion API rejected the token"}


def _test_storygraph(enabled: bool, session_cookie: str, remember_user_token: str) -> dict:
    if not enabled:
        return {"ok": False, "message": "StoryGraph is disabled"}
    if not session_cookie or not remember_user_token:
        return {"ok": False, "message": "Missing StoryGraph session cookies"}

    cookie = f"_storygraph_session={session_cookie}; remember_user_token={remember_user_token}"
    r = requests.get(
        "https://app.thestorygraph.com/users/sign_in",
        headers={
            "Cookie": cookie,
            "User-Agent": "ABS-KoSync-Bridge/StoryGraph",
        },
        timeout=10,
        allow_redirects=False,
    )
    location = (r.headers.get("Location") or r.headers.get("location") or "").lower()
    if r.status_code in (302, 303) and "/users/sign_in" not in location:
        return {"ok": True, "message": "StoryGraph session accepted"}
    if r.status_code in (200, 401, 403):
        return {"ok": False, "message": "Invalid StoryGraph session cookies"}
    if r.status_code in (302, 303) and "/users/sign_in" in location:
        return {"ok": False, "message": "Invalid StoryGraph session cookies"}
    return {"ok": False, "message": f"StoryGraph returned {r.status_code}"}


def _test_telegram(enabled: bool, token: str) -> dict:
    if not enabled or not token:
        return {"ok": False, "message": "Telegram not configured or disabled"}
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
    if r.status_code == 200 and r.json().get('ok'):
        bot_name = r.json().get('result', {}).get('username', 'unknown')
        return {"ok": True, "message": f"Connected (bot: @{bot_name})"}
    if r.status_code == 401:
        return {"ok": False, "message": "Invalid bot token"}
    return {"ok": False, "message": f"Telegram API returned {r.status_code}"}

# ---------------- HELPER FUNCTIONS ----------------
def safe_folder_name(name: str) -> str:
    """Sanitize folder name for file system safe usage."""
    invalid = '<>:"/\\|?*'
    name = html.escape(str(name).strip())[:150]
    for c in invalid:
        name = name.replace(c, '_')
    return name.strip() or "Unknown"

def _resolve_web_secret_key() -> str:
    """Resolve the Flask session-signing key (which signs the auth identity).

    Order: WEB_SECRET_KEY env -> a key persisted in the settings table -> a fresh
    random key (persisted if possible). Never falls back to a shared constant, so
    an attacker can't forge a session cookie on an install that didn't set the env.
    """
    import secrets
    key = os.environ.get("WEB_SECRET_KEY")
    if key:
        return key
    try:
        stored = database_service.get_setting("WEB_SECRET_KEY") if database_service else None
        if isinstance(stored, str) and stored:
            return stored
    except Exception:
        pass
    new_key = secrets.token_hex(32)
    try:
        if database_service:
            database_service.set_setting("WEB_SECRET_KEY", new_key)
    except Exception:
        pass
    return new_key


@admin_required
def api_diagnostics_send_now():
    """Submit a manual diagnostics report for this BookBridge instance."""
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({'error': 'Request body must be a JSON object.'}), 400
    message = body.get('message', '')
    if not isinstance(message, str):
        return jsonify({'error': 'Message must be a string.'}), 400
    message = message.strip()
    if len(message) > 2000:
        return jsonify({'error': 'Message must be 2000 characters or fewer.'}), 400

    result = _run_diagnostics_send(
        force=True,
        manual=True,
        user_message=message,
    )
    if result.get('sent') or result.get('reason') in ('opt_out', 'no_endpoint'):
        return jsonify(result), 200
    if result.get('reason') == 'http_429':
        return jsonify(result), 429
    return jsonify(result), 502


@admin_required
def api_diagnostics_opt_in():
    """Admin endpoint: toggle diagnostics opt-in state."""
    body = request.get_json(silent=True) or {}
    opted_in = bool(body.get('opt_in'))
    val = 'true' if opted_in else 'false'
    database_service.set_setting('DIAGNOSTICS_OPT_IN', val)
    os.environ['DIAGNOSTICS_OPT_IN'] = val
    database_service.set_setting('DIAGNOSTICS_PROMPTED', 'true')
    os.environ['DIAGNOSTICS_PROMPTED'] = 'true'

    instance_id = ''
    if opted_in:
        from src.services import diagnostics
        instance_id = diagnostics.ensure_instance_id(database_service)

    return jsonify({'ok': True, 'opt_in': opted_in, 'instance_id': instance_id})


def _diagnostics_receiver_context():
    """Validate settings and derive the receiver base URL for proxied requests.

    Returns ``(base_url, token, error_message)``.  ``base_url`` is
    ``scheme://netloc`` parsed from ``DIAGNOSTICS_ENDPOINT_URL`` (no
    attacker-controlled path component).  ``token`` is the raw ingest
    token.  When either setting is missing/empty *error_message* is a
    human-readable string and both URL and token are empty.
    """
    endpoint = os.environ.get('DIAGNOSTICS_ENDPOINT_URL', '').strip()
    token = os.environ.get('DIAGNOSTICS_INGEST_TOKEN', '').strip()
    if not endpoint:
        return '', '', 'Diagnostics endpoint is not configured.'
    if not token:
        return '', '', 'Diagnostics ingest token is not configured.'
    parsed = urlparse(endpoint)
    base = (
        f'{parsed.scheme}://{parsed.netloc}'
        if parsed.scheme in ('http', 'https') and parsed.netloc else ''
    )
    if not base:
        return '', '', 'Diagnostics endpoint URL is invalid.'
    return base, token, ''


@admin_required
def my_reports():
    """Redirect the retired technical reports page to Diagnostics settings."""
    return redirect(url_for('settings') + '#system')


@admin_required
def api_diagnostics_submissions():
    """Return this instance's manual report history without exposing its token."""
    base_url, token, error = _diagnostics_receiver_context()
    if error:
        if error == 'Diagnostics ingest token is not configured.':
            return jsonify({'submissions': []})
        return jsonify({'error': error}), 503

    headers = {'Authorization': f'Bearer {token}'}
    try:
        resp = requests.get(
            f'{base_url}/api/v1/my/submissions', headers=headers, timeout=15,
        )
    except requests.RequestException:
        return jsonify({'error': 'Could not reach the diagnostics receiver.'}), 502

    if resp.status_code != 200:
        return jsonify({'error': 'The diagnostics receiver returned an error.'}), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({'error': 'The diagnostics receiver returned an invalid response.'}), 502
    if not isinstance(data, dict) or not isinstance(data.get('submissions'), list):
        return jsonify({'error': 'The diagnostics receiver returned an invalid response.'}), 502

    submissions = []
    for item in data['submissions']:
        if not isinstance(item, dict):
            continue
        user_message = item.get('user_message')
        response_md = item.get('response_md')
        submitted_at = item.get('submitted_at') or item.get('received_at')
        submissions.append({
            'id': item.get('id'),
            'submitted_at': submitted_at if isinstance(submitted_at, str) else '',
            'user_message': user_message if isinstance(user_message, str) else '',
            'response_md': response_md if isinstance(response_md, str) else '',
            'response_at': item.get('response_at') if isinstance(item.get('response_at'), str) else '',
            'status': 'replied' if (
                item.get('status') == 'replied'
                or isinstance(response_md, str) and bool(response_md.strip())
            ) else 'received',
        })
    return jsonify({'submissions': submissions})


# --- Application Factory ---
def create_app(test_container=None):
    # Under a test container, run deferred work inline so integration tests are
    # deterministic (no background thread races).
    if test_container is not None:
        global _BACKGROUND_TASKS_SYNCHRONOUS
        _BACKGROUND_TASKS_SYNCHRONOUS = True
    # The drift memo is process-scoped and keyed on stored positions; a new app
    # instance is a new process's worth of state, so it starts empty.
    _DASHBOARD_SYNC_WARNING_CACHE.clear()
    STATIC_DIR = os.environ.get('STATIC_DIR', '/app/static')
    TEMPLATE_DIR = os.environ.get('TEMPLATE_DIR', '/app/templates')
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='/static', template_folder=TEMPLATE_DIR)
    # Harden the session cookie (it signs the auth identity). SECURE is opt-in so
    # plain-HTTP LAN deployments still work; enable behind TLS via env.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if str(os.environ.get("SESSION_COOKIE_SECURE", "")).strip().lower() in ("true", "1", "yes", "on"):
        app.config["SESSION_COOKIE_SECURE"] = True

    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    # Tests inject a test_container and exercise routes without a session; honor
    # Flask's conventional LOGIN_DISABLED so the auth guard is a no-op there.
    # Production (no test_container) always enforces auth. CSRF protection is
    # tied to the same switch so tests can POST to authed routes without tokens.
    app.config['CSRF_ENABLED'] = True
    if test_container is not None:
        app.config['LOGIN_DISABLED'] = True
        app.config['CSRF_ENABLED'] = False

    # Setup dependencies and inject into app context
    setup_dependencies(app, test_container=test_container)

    # Resolve the session-signing key AFTER the DB is wired so it can persist a
    # generated key (survives restarts; no hardcoded fallback).
    app.secret_key = _resolve_web_secret_key()

    # Multi-user: require a web session for all UI routes (device KoSync sync
    # blueprint and auth/health endpoints are exempted inside the guard).
    app.before_request(require_login_guard)
    app.before_request(csrf_protect_guard)
    app.teardown_request(_release_request_user_context)
    app.after_request(inject_csrf_script)

    # Register context processors, jinja globals, etc.
    app.context_processor(inject_global_vars)
    app.jinja_env.globals['safe_folder_name'] = safe_folder_name
    app.jinja_env.globals['suggestion_source_badge'] = suggestion_source_badge

    def format_duration(seconds: int) -> str:
        """Convert seconds to human-readable duration."""
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"

    def format_time_ago(unix_timestamp: float) -> str:
        """Convert a unix timestamp to relative time from now."""
        import time as _time
        diff = _time.time() - unix_timestamp
        if diff < 60:
            return f"{int(diff)}s"
        if diff < 3600:
            return f"{int(diff // 60)}m"
        if diff < 86400:
            return f"{int(diff // 3600)}h"
        return f"{int(diff // 86400)}d"

    app.jinja_env.filters['format_duration'] = format_duration
    app.jinja_env.filters['format_time_ago'] = format_time_ago

    def _legacy_book_linker_redirect(dummy=None):
        return redirect(url_for('add_book'), code=301)

    # Register all routes here
    app.add_url_rule('/setup', 'setup', setup, methods=['GET', 'POST'])
    app.add_url_rule('/login', 'login', login, methods=['GET', 'POST'])
    app.add_url_rule('/logout', 'logout', logout, methods=['GET', 'POST'])
    app.add_url_rule('/account', 'account', account, methods=['GET', 'POST'])
    app.add_url_rule('/account/integrations', 'account_integrations', account_integrations, methods=['GET', 'POST'])
    app.add_url_rule('/api/account/test-connection/<service>', 'account_test_connection', account_test_connection, methods=['POST'])
    app.add_url_rule('/api/account/abs-libraries', 'account_abs_libraries', account_abs_libraries, methods=['POST'])
    app.add_url_rule('/api/account/booklore-libraries', 'account_booklore_libraries', account_booklore_libraries, methods=['POST'])
    app.add_url_rule('/admin/users', 'admin_users', admin_users, methods=['GET', 'POST'])
    app.add_url_rule('/admin/users/<int:user_id>/integrations', 'admin_user_integrations', admin_user_integrations, methods=['GET', 'POST'])
    app.add_url_rule('/api/admin/users/<int:user_id>/test-connection/<service>', 'admin_user_test_connection', admin_user_test_connection, methods=['POST'])
    app.add_url_rule('/api/admin/users/<int:user_id>/abs-libraries', 'admin_user_abs_libraries', admin_user_abs_libraries, methods=['POST'])
    app.add_url_rule('/api/admin/users/<int:user_id>/booklore-libraries', 'admin_user_booklore_libraries', admin_user_booklore_libraries, methods=['POST'])
    app.add_url_rule('/api/admin/users/<int:user_id>/bookfusion/device/start', 'admin_user_bookfusion_device_start', admin_user_bookfusion_device_start, methods=['POST'])
    app.add_url_rule('/api/admin/users/<int:user_id>/bookfusion/device/poll', 'admin_user_bookfusion_device_poll', admin_user_bookfusion_device_poll, methods=['POST'])
    app.add_url_rule('/', 'index', index)
    app.add_url_rule('/shelfmark', 'shelfmark', shelfmark)
    app.add_url_rule('/forge', 'forge', forge)
    app.add_url_rule('/book-linker', 'book_linker_legacy', _legacy_book_linker_redirect)
    app.add_url_rule('/book-linker/<path:dummy>', 'book_linker_legacy_path', _legacy_book_linker_redirect)
    app.add_url_rule('/match', 'match', match, methods=['GET', 'POST'])
    app.add_url_rule('/add-book', 'add_book', add_book, methods=['GET', 'POST'])
    app.add_url_rule('/batch-match', 'batch_match', batch_match, methods=['GET', 'POST'])
    app.add_url_rule('/suggestions', 'suggestions', suggestions_page, methods=['GET', 'POST'])
    app.add_url_rule('/delete/<abs_id>', 'delete_mapping', delete_mapping, methods=['POST'])
    app.add_url_rule('/clear-progress/<abs_id>', 'clear_progress', clear_progress, methods=['POST'])
    app.add_url_rule('/api/remap-alignment/<abs_id>', 'remap_alignment', remap_alignment, methods=['POST'])
    app.add_url_rule('/api/sync-now/<abs_id>', 'sync_now', sync_now, methods=['POST'])
    app.add_url_rule('/api/mark-complete/<abs_id>', 'mark_complete', mark_complete, methods=['POST'])
    app.add_url_rule('/api/me/kosync-documents', 'api_me_kosync_documents', api_me_kosync_documents, methods=['GET'])
    app.add_url_rule('/api/me/kosync-documents/<doc_hash>/link', 'api_me_link_kosync_document', api_me_link_kosync_document, methods=['POST'])
    app.add_url_rule('/api/me/kosync-documents/<doc_hash>/unlink', 'api_me_unlink_kosync_document', api_me_unlink_kosync_document, methods=['POST'])
    app.add_url_rule('/api/me/kosync-documents/<doc_hash>', 'api_me_delete_kosync_document', api_me_delete_kosync_document, methods=['DELETE'])
    app.add_url_rule('/api/me/books', 'api_me_books', api_me_books, methods=['GET'])
    app.add_url_rule('/update-hash/<abs_id>', 'update_hash', update_hash, methods=['POST'])
    app.add_url_rule('/covers/<path:filename>', 'serve_cover', serve_cover)
    app.add_url_rule('/api/health', 'api_health', api_health)
    app.add_url_rule('/api/restart', 'api_restart', api_restart, methods=['POST'])
    app.add_url_rule('/api/status', 'api_status', api_status)
    app.add_url_rule('/api/status/progress', 'api_status_progress', api_status_progress)
    app.add_url_rule('/stats', 'stats_view', stats_view)
    app.add_url_rule('/api/stats', 'api_stats', api_stats)
    app.add_url_rule('/api/stats/reading-day', 'api_stats_reading_day', api_stats_reading_day)
    app.add_url_rule('/api/stats/reading-calendar', 'api_stats_reading_calendar', api_stats_reading_calendar)
    app.add_url_rule('/api/stats/book-detail', 'api_stats_book_detail', api_stats_book_detail)
    app.add_url_rule('/api/stats/yearly-recap', 'api_stats_yearly_recap', api_stats_yearly_recap)
    app.add_url_rule('/logs', 'logs_view', logs_view)
    app.add_url_rule('/api/logs', 'api_logs', api_logs)
    app.add_url_rule('/api/logs/live', 'api_logs_live', api_logs_live)
    app.add_url_rule('/view_log', 'view_log', view_log)
    app.add_url_rule('/settings', 'settings', settings, methods=['GET', 'POST'])

    # Suggestion routes
    app.add_url_rule('/api/suggestions', 'get_suggestions', get_suggestions, methods=['GET'])
    app.add_url_rule('/api/suggestions/scan-status', 'suggestions_scan_status', suggestions_scan_status, methods=['GET'])
    app.add_url_rule('/api/suggestions/<source_id>/dismiss', 'dismiss_suggestion', dismiss_suggestion, methods=['POST'])
    app.add_url_rule('/api/suggestions/<source_id>/ignore', 'ignore_suggestion', ignore_suggestion, methods=['POST'])
    app.add_url_rule('/api/suggestions/clear_stale', 'clear_stale_suggestions', clear_stale_suggestions, methods=['POST'])
    app.add_url_rule('/api/cache/clean', 'clean_cache', clean_inactive_cache, methods=['POST'])
    app.add_url_rule('/api/cover-proxy/<abs_id>', 'proxy_cover', proxy_cover)
    app.add_url_rule('/api/booklore/audiobook-cover/<book_id>', 'proxy_booklore_audiobook_cover', proxy_booklore_audiobook_cover, methods=['GET'])
    app.add_url_rule('/api/bookorbit/audiobook-cover/<book_id>', 'proxy_bookorbit_audiobook_cover', proxy_bookorbit_audiobook_cover, methods=['GET'])
    app.add_url_rule('/api/kavita/cover/<series_id>', 'proxy_kavita_cover', proxy_kavita_cover, methods=['GET'])
    app.add_url_rule('/api/booklore/libraries', 'get_booklore_libraries', get_booklore_libraries, methods=['GET'])
    app.add_url_rule('/api/booklore/shelves', 'get_booklore_shelves', get_booklore_shelves, methods=['GET'])
    app.add_url_rule('/api/abs/libraries', 'get_abs_libraries', get_abs_libraries, methods=['GET'])
    app.add_url_rule('/api/booklore/refresh', 'api_booklore_refresh', api_booklore_refresh, methods=['POST'])
    app.add_url_rule('/api/bookfusion/device/start', 'api_bookfusion_device_start', api_bookfusion_device_start, methods=['POST'])
    app.add_url_rule('/api/bookfusion/device/poll', 'api_bookfusion_device_poll', api_bookfusion_device_poll, methods=['POST'])
    app.add_url_rule('/api/bookfusion/search', 'api_bookfusion_search', api_bookfusion_search, methods=['GET'])
    app.add_url_rule('/api/bookfusion/link/<abs_id>', 'api_bookfusion_link', api_bookfusion_link, methods=['POST'])
    app.add_url_rule('/api/bookfusion/link/<abs_id>', 'api_bookfusion_unlink', api_bookfusion_unlink, methods=['DELETE'])
    app.add_url_rule('/api/bookfusion/upload/<abs_id>', 'api_bookfusion_upload', api_bookfusion_upload, methods=['POST'])
    app.add_url_rule('/api/test-connection/<service>', 'test_connection', test_connection, methods=['POST'])
    app.add_url_rule('/api/diagnostics/send-now', 'api_diagnostics_send_now', api_diagnostics_send_now, methods=['POST'])
    app.add_url_rule('/api/diagnostics/opt-in', 'api_diagnostics_opt_in', api_diagnostics_opt_in, methods=['POST'])
    app.add_url_rule('/my-reports', 'my_reports', my_reports, methods=['GET'])
    app.add_url_rule('/api/diagnostics/submissions', 'api_diagnostics_submissions', api_diagnostics_submissions, methods=['GET'])

    # Storyteller API routes
    app.add_url_rule('/api/storyteller/search', 'api_storyteller_search', api_storyteller_search, methods=['GET'])
    app.add_url_rule('/api/storyteller/link/<abs_id>', 'api_storyteller_link', api_storyteller_link, methods=['POST'])
    app.add_url_rule('/api/storyteller/backfill', 'api_storyteller_backfill', api_storyteller_backfill, methods=['POST'])
    app.add_url_rule('/api/admin/backfill-series', 'api_series_backfill', api_series_backfill, methods=['POST'])
    app.add_url_rule('/api/admin/audio-repoint/plan', 'api_audio_repoint_plan', api_audio_repoint_plan, methods=['GET', 'POST'])
    app.add_url_rule('/api/admin/audio-repoint/apply', 'api_audio_repoint_apply', api_audio_repoint_apply, methods=['POST'])
    app.add_url_rule('/api/admin/audio-repoint/undo', 'api_audio_repoint_undo', api_audio_repoint_undo, methods=['POST'])
    app.add_url_rule('/api/admin/debug-abs-series', 'api_debug_abs_series', api_debug_abs_series, methods=['GET'])

    # Forge routes
    app.add_url_rule('/api/forge/search_audio', 'forge_search_audio', forge_search_audio, methods=['GET'])
    app.add_url_rule('/api/forge/search_text', 'forge_search_text', forge_search_text, methods=['GET'])
    app.add_url_rule('/api/forge/process', 'forge_process', forge_process, methods=['POST'])
    app.add_url_rule('/api/alignments/llm-status', 'alignments_llm_status', alignments_llm_status, methods=['GET'])
    app.add_url_rule('/api/alignments/realign', 'alignments_realign', alignments_realign, methods=['POST'])
    app.add_url_rule('/api/alignments/restore', 'alignments_restore', alignments_restore, methods=['POST'])

    @app.route('/api/forge/active', methods=['GET'])
    def forge_active_tasks():
        """Return active forging tasks, scoped to the current user for non-admins."""
        user = current_user()
        is_admin = user is not None and getattr(user, 'is_admin', False)
        is_authenticated = user is not None

        tasks = set()

        # Unauthenticated or admin: show all forge_service internal tasks
        if not is_authenticated or is_admin:
            try:
                tasks.update(container.forge_service().active_tasks or set())
            except Exception:
                pass

        try:
            forging_books = database_service.get_books_by_status('forging')
            if isinstance(forging_books, (list, tuple, set)):
                if is_authenticated and not is_admin:
                    # Non-admins only see forging books they have claimed
                    linked_ids = database_service.get_linked_abs_ids(user.id)
                    forging_books = [b for b in forging_books if getattr(b, 'abs_id', None) in linked_ids]
                for book in forging_books:
                    title = getattr(book, 'abs_title', None) or getattr(book, 'audio_title', None) or getattr(book, 'abs_id', None)
                    if title:
                        tasks.add(title)
        except Exception as exc:
            logger.debug("Forge active task lookup failed: %s", exc)
        return jsonify(sorted(tasks))

    # Return both app and container for external reference
    return app, container

# ---------------- MAIN ----------------
if __name__ == '__main__':

    # Setup signal handlers to catch unexpected kills
    import signal
    def handle_exit_signal(signum, frame):
        logger.warning(f"⚠️ Received signal {signum} - Shutting down...")
        # Flush logs immediately
        for handler in logger.handlers:
            handler.flush()
        if hasattr(logging.getLogger(), 'handlers'):
            for handler in logging.getLogger().handlers:
                handler.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_exit_signal)
    signal.signal(signal.SIGINT, handle_exit_signal)

    app, container = create_app()

    logger.info("=== Unified ABS Manager Started (Integrated Mode) ===")

    # Start sync daemon in background thread
    sync_daemon_thread = threading.Thread(target=sync_daemon, daemon=True)
    sync_daemon_thread.start()
    threading.Thread(target=get_update_status, daemon=True).start()
    logger.info("🚀 Sync daemon thread started")

    # Start ABS Socket.IO listener for real-time / instant sync
    instant_sync_enabled = os.environ.get('INSTANT_SYNC_ENABLED', 'true').lower() != 'false'
    abs_socket_enabled = os.environ.get('ABS_SOCKET_ENABLED', 'true').lower() != 'false'
    if instant_sync_enabled and abs_socket_enabled and container.abs_client().is_configured():
        from src.services.abs_socket_manager import ABSSocketManager
        abs_socket_manager = ABSSocketManager(
            database_service=database_service,
            sync_manager=manager,
            user_client_registry=container.user_client_registry(),
        )
        abs_socket_manager.start()
        logger.info("🔌 ABS Socket.IO listeners started (instant sync enabled, per-user)")
    elif not instant_sync_enabled:
        logger.info("ℹ️ ABS Socket.IO listener disabled (INSTANT_SYNC_ENABLED=false)")
    elif not abs_socket_enabled:
        logger.info("ℹ️ ABS Socket.IO listener disabled (ABS_SOCKET_ENABLED=false)")

    # Start per-client poller (always-on; _poll_cycle skips clients in 'global' mode)
    from src.services.client_poller import ClientPoller
    client_poller = ClientPoller(
        database_service=database_service,
        sync_manager=manager,
        sync_clients_dict=container.sync_clients(),
        shelf_watch_services=container.shelf_watch_services_by_client(),
        user_client_registry=container.user_client_registry(),
    )
    poller_thread = threading.Thread(target=client_poller.start, daemon=True)
    poller_thread.start()

    # Annotation hub — BookOrbit spoke: relay highlights between devices and
    # BookOrbit's web reader for users with BOOKORBIT_KOSYNC_* creds configured.
    try:
        from src.services.annotation_sync_service import AnnotationSyncService, run_annotation_sync_daemon

        def _annotation_sync_interval():
            intervals = []
            try:
                bookorbit_interval = int(os.environ.get("BOOKORBIT_ANNOTATION_SYNC_MINUTES", "15") or 0)
                if bookorbit_interval > 0:
                    intervals.append(bookorbit_interval)
            except (TypeError, ValueError):
                pass
            try:
                booklore_interval = int(os.environ.get("BOOKLORE_ANNOTATION_SYNC_MINUTES", "15") or 0)
                if booklore_interval > 0:
                    intervals.append(booklore_interval)
            except (TypeError, ValueError):
                pass
            try:
                readest_interval = int(os.environ.get("READEST_ANNOTATION_SYNC_MINUTES", "15") or 0)
                if readest_interval > 0:
                    intervals.append(readest_interval)
            except (TypeError, ValueError):
                pass
            try:
                hardcover_interval = int(os.environ.get("HARDCOVER_ANNOTATION_SYNC_MINUTES", "30") or 0)
                if hardcover_interval > 0:
                    intervals.append(hardcover_interval)
            except (TypeError, ValueError):
                pass
            # The Readest "currently reading" upload sweep rides this same daemon
            # rather than starting its own thread. Without a contribution here, a
            # user who enables uploads but no annotation sync would get 0 back
            # from this function and the daemon would never run a cycle at all.
            # Since the result is a min(), adding this never slows an existing
            # schedule down.
            try:
                readest_upload_interval = int(os.environ.get("READEST_UPLOAD_SWEEP_MINUTES", "60") or 0)
                if readest_upload_interval > 0:
                    intervals.append(readest_upload_interval)
            except (TypeError, ValueError):
                pass
            return min(intervals) if intervals else 0

        annotation_sync_service = AnnotationSyncService(
            database_service,
            ebook_parser=container.ebook_parser(),
            epub_cache_dir=container.epub_cache_dir(),
        )
        threading.Thread(
            target=run_annotation_sync_daemon,
            args=(annotation_sync_service, _annotation_sync_interval),
            daemon=True,
            name="annotation-sync",
        ).start()
    except Exception as exc:
        logger.warning("Annotation sync daemon failed to start: %s", exc, exc_info=True)

    # Keep KoSync hashes bound to their books after a library file is edited. The
    # manifest prebuilder does this too, but only once a device has asked for a
    # manifest (ref #342), so installs that never use device-sync would otherwise
    # never re-link a drifted hash.
    try:
        from src.services.hash_reconciler import start_hash_reconciler_thread
        start_hash_reconciler_thread(
            container.koreader_device_sync_service(),
            user_client_registry=container.user_client_registry(),
            database_service=database_service,
        )
    except Exception as exc:
        logger.warning("Hash reconciler failed to start: %s", exc, exc_info=True)

    # Re-attach Forge & Match completion watchers orphaned by a restart. The
    # banner/card survive in the DB (status='forging'), but the polling thread
    # that finalizes the forge does not, so resume it here. The registry lets
    # each book resume on its owner's clients (multi-user), not just the admin's.
    try:
        container.forge_service().resume_pending_forge_matches(
            user_client_registry=container.user_client_registry()
        )
    except Exception as exc:
        logger.warning("Forge & Match: resume on startup failed: %s", exc, exc_info=True)

    # One-time backfill of StoryGraph ratings for already-linked books.
    # Self-limiting: rows with storygraph_rating_updated_at set are skipped on future startups.
    try:
        from src.services.storygraph_rating_backfill import start_backfill_thread as _start_sg_backfill
        _start_sg_backfill(
            database_service=database_service,
            storygraph_client=container.storygraph_client(),
        )
    except Exception as exc:
        logger.warning("StoryGraph rating backfill thread failed to start: %s", exc, exc_info=True)

    # Check ebook source configuration
    booklore_configured = container.booklore_client().is_configured()
    bookorbit_configured = container.bookorbit_client().is_configured()
    kavita_configured = container.kavita_client().is_configured()
    books_volume_exists = container.books_dir().exists()

    if booklore_configured:
        logger.info(f"✅ Grimmory integration enabled - ebooks sourced from API")
    elif bookorbit_configured:
        logger.info(f"✅ BookOrbit integration enabled - ebooks sourced from API")
    elif kavita_configured:
        logger.info("Kavita integration enabled - ebooks sourced from API")
    elif books_volume_exists:
        logger.info(f"✅ Ebooks directory mounted at {container.books_dir()}")
    else:
        logger.info(
            "⚠️  NO EBOOK SOURCE CONFIGURED: No ebook source available. "
            "New book matches will fail. Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD), "
            "enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, BOOKORBIT_PASSWORD), "
            "enable Kavita (KAVITA_SERVER, KAVITA_API_KEY), "
            "or mount the ebooks directory to /books."
        )


    logger.info(f"🌐 Web interface starting on port 5757")

    # --- Split-Port Mode ---
    sync_port = os.environ.get('KOSYNC_PORT')
    if sync_port and int(sync_port) != 5757:
        def run_sync_only_server(port):
            sync_app = Flask(__name__)
            sync_app.register_blueprint(kosync_sync_bp)
            @sync_app.route('/')
            def sync_health():
                return "Sync Server OK", 200
            sync_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

        threading.Thread(target=run_sync_only_server, args=(int(sync_port),), daemon=True).start()
        logger.info(f"🚀 Split-Port Mode Active: Sync-only server on port {sync_port}")

    app.run(host='0.0.0.0', port=5757, debug=False)
