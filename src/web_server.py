# [START FILE: abs-kosync-enhanced/web_server.py]
import glob
import hmac
import html
import logging
import json
import contextvars
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
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

from src.utils.config_loader import ConfigLoader, env_truthy
from src.utils.cache_paths import safe_cache_path
from src.utils.logging_utils import memory_log_handler, LOG_PATH
from src.utils.logging_utils import sanitize_log_data
from src.api.api_clients import ABS_DISABLED_SENTINEL, is_abs_disabled_value
from src.api.kosync_server import kosync_sync_bp, kosync_admin_bp, init_kosync_server, signal_manifest_rebuild
from src.api.hardcover_routes import hardcover_bp, init_hardcover_routes
from src.api.storygraph_routes import storygraph_bp, init_storygraph_routes
from src.version import APP_VERSION, get_update_status
from src.db.models import State
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.services.audio_source_adapters import AudioResult, ABSAudioSourceAdapter, BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter
from src.utils.storyteller_transcript import StorytellerTranscript
from src.utils.kosync_headers import hash_kosync_key, kosync_auth_headers

def _reconfigure_logging():
    """Force update of root logger level based on env var."""
    try:
            new_level_str = os.environ.get('LOG_LEVEL', 'INFO').upper()
            new_level = getattr(logging, new_level_str, logging.INFO)

            root = logging.getLogger()
            root.setLevel(new_level)

            logger.info(f"📝 Logging level updated to {new_level_str}")
    except Exception as e:
            logger.warning(f"⚠️ Failed to reconfigure logging: {e}")

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
        ConfigLoader.load_settings(database_service)
        logger.info("✅ Settings loaded into environment variables")

        # Force reconfigure logging level based on new settings
        _reconfigure_logging()

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
            logger.warning(f"⚠️ Invalid '{key}' value, defaulting to {default}")
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
        logger.warning(f"Could not wire shelf_watch_service suggestions factory: {e}")

    # Get data directories (now using updated env vars)
    DATA_DIR = container.data_dir()
    EBOOK_DIR = container.books_dir()

    # Initialize covers directory
    COVERS_DIR = DATA_DIR / "covers"
    if not COVERS_DIR.exists():
        COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Register KoSync Blueprint and initialize with dependencies
    init_kosync_server(database_service, container, manager, EBOOK_DIR)
    manager.register_post_cycle_callback(signal_manifest_rebuild)
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
    'api_booklore_refresh', 'alignments_llm_status', 'alignments_realign',
    'kosync_admin.api_get_kosync_documents',
    'kosync_admin.api_link_kosync_document',
    'kosync_admin.api_unlink_kosync_document',
    'kosync_admin.api_delete_kosync_document',
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
                    logger.warning("Deferred Hardcover automatch failed for '%s': %s", abs_id, e)
            storygraph = sync_clients.get("StoryGraph")
            if storygraph and storygraph.is_configured():
                try:
                    storygraph._automatch_storygraph(book)
                except Exception as e:
                    logger.warning("Deferred StoryGraph automatch failed for '%s': %s", abs_id, e)
        except Exception as e:
            logger.error("Deferred tracker automatch worker error: %s", e)
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
            logger.error("%s failed: %s", label, e)
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
            logger.error("%s failed: %s", label, e)
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
    # admins inherit the global config; regular users are isolated. This ambient
    # dict is also what _spawn_user_background copies onto worker threads.
    creds = dict(creds)
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = bool(getattr(user, "is_admin", False))
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

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = None
        if database_service is not None:
            user = database_service.verify_user_credentials(username, password)
        if user:
            return _establish_session_and_redirect(user)
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
        logger.error("Could not inspect users for setup: %s", e)
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
                logger.error("Initial admin setup failed: %s", e)
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
        allow_master_fallback=bool(getattr(user, "is_admin", False)),
        message=message,
        account_user=user,
        user_test_services=_USER_TEST_SERVICES,
    )


# Library-lookup credentials the primary admin's account also lends to the
# engine's global singletons (shelf-watch, scans, suggestions, ABS socket,
# manifest). Mirrored to the global settings when the primary admin saves.
_ENGINE_MIRROR_KEYS = (
    "ABS_KEY", "ABS_LIBRARY_ID",
    "BOOKLORE_USER", "BOOKLORE_PASSWORD", "BOOKLORE_SHELF_NAME", "BOOKLORE_LIBRARY_ID",
    "BOOKORBIT_USER", "BOOKORBIT_PASSWORD", "BOOKORBIT_SHELF_NAME",
    "CWA_USERNAME", "CWA_PASSWORD", "CWA_SYNC_TOKEN",
)


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
            # Only mirror real values: a blank text field would otherwise clobber a
            # configured global value with '' and defeat os.environ.get(key, default).
            if val:
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
        allow_master_fallback=bool(getattr(target, "is_admin", False)),
        message=message,
        target_user=target,
        user_test_services=_USER_TEST_SERVICES,
    )


_USER_TEST_SERVICES = {
    "Audiobookshelf": "abs",
    "KOReader / KoSync": "kosync",
    "Storyteller": "storyteller",
    "Calibre-Web (Automated)": "cwa",
    "BookOrbit": "bookorbit",
    "Grimmory / BookLore": "booklore",
    "Readest": "readest",
    "BookFusion": "bookfusion",
    "Hardcover": "hardcover",
    "StoryGraph": "storygraph",
}


_TEST_CONNECTION_FIELDS = {
    'abs': ['ABS_SERVER', 'ABS_KEY'],
    'kosync': ['KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY'],
    'storyteller': ['STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD'],
    'booklore': ['BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD'],
    'bookorbit': ['BOOKORBIT_ENABLED', 'BOOKORBIT_SERVER', 'BOOKORBIT_USER', 'BOOKORBIT_PASSWORD'],
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
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = bool(getattr(target, "is_admin", False))

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
                payload[key] = resolve_setting(creds, key, "")
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
        logger.warning("Per-user ABS library lookup failed for user %s: %s", user_id, e)
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
        logger.warning("Self-service ABS library lookup failed for user %s: %s", user.id, e)
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
        logger.warning("Per-user Grimmory library lookup failed for user %s: %s", user_id, e)
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
        logger.warning("Self-service Grimmory library lookup failed for user %s: %s", user.id, e)
        return jsonify({"error": str(e)}), 502


# User-management actions accepted by both the legacy /admin/users page and the
# Settings → Users tab (which posts to /settings).
_USER_ADMIN_ACTIONS = {'create', 'reset_password', 'toggle_active', 'delete'}


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
                database_service.create_user(username, password, role=role)
                message = f"Created user '{username}'"
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
                database_service.delete_user(uid)
                try:
                    container.user_client_registry().invalidate(uid)
                except Exception:
                    pass
                message = f"Deleted '{target.username}'"
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
    return render_template(
        'admin_users.html',
        users=users,
        message=message,
        error=error,
        current_user_id=current_user().id,
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

    return dict(
        shelfmark_url=os.environ.get("SHELFMARK_URL", ""),
        abs_server=_display_abs_server(),
        booklore_server=os.environ.get("BOOKLORE_SERVER", ""),
        get_val=get_val,
        get_bool=get_bool,
        get_user_val=get_user_val,
        get_user_bool=get_user_bool,
        current_user=current_user(),
    )

# ---------------- BOOK LINKER HELPERS ----------------
from src.services.alignment_service import ingest_storyteller_transcripts









def sync_daemon():
    """Background sync daemon running in a separate thread."""
    try:
        # Setup schedule for sync operations
        # Use the global SYNC_PERIOD_MINS which is validated
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(manager.run_sync_for_all_users)
        schedule.every(1).minutes.do(manager.check_pending_jobs)

        logger.info(f"🔄 Sync daemon started (period: {SYNC_PERIOD_MINS} minutes)")

        # Run initial sync cycle (per user)
        try:
            manager.run_sync_for_all_users()
        except Exception as e:
            logger.error(f"❌ Initial sync cycle failed: {e}")

        # Main daemon loop
        while True:
            try:
                # logger.debug("Running pending schedule jobs...")
                schedule.run_pending()
                time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                logger.error(f"❌ Sync daemon error: {e}")
                time.sleep(60)  # Wait longer on error

    except Exception as e:
        logger.error(f"❌ Sync daemon crashed: {e}")


# ---------------- ORIGINAL ABS-KOSYNC HELPERS ----------------

def find_ebook_file(filename):
    base = EBOOK_DIR
    escaped_filename = glob.escape(filename)
    matches = list(base.rglob(escaped_filename))
    return matches[0] if matches else None


def get_kosync_id_for_ebook(ebook_filename, booklore_id=None, original_filename=None,
                            bookorbit_id=None, source_path=None):
    """Get KOSync document ID for an ebook.
    Tries Grimmory API first (if configured and booklore_id provided),
    falls back to filesystem, then BookOrbit / ABS / CWA on-demand downloads.
    """
    # Try Grimmory API first
    clients = uc()

    if booklore_id and clients.booklore_client.is_configured():
        try:
            content = clients.booklore_client.download_book(booklore_id)
            if content:
                kosync_id = container.ebook_parser().get_kosync_id_from_bytes(ebook_filename, content)
                if kosync_id:
                    logger.debug(f"🔍 Computed KOSync ID from Grimmory download: '{kosync_id}'")
                    return kosync_id
        except Exception as e:
            logger.warning(f"⚠️ Failed to get KOSync ID from Grimmory, falling back to filesystem: {e}")

    # Fall back to filesystem. When a queue item carries the selected local source path,
    # prefer it over filename-only globbing so duplicate basenames do not hash the wrong book.
    ebook_path = None
    source_path_text = str(source_path or "").strip()
    if source_path_text and "://" not in source_path_text:
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

    # [NEW] Check Epub Cache explicitly (if acquired by LibraryService but not meant for /books)
    epub_cache = container.epub_cache_dir()
    cached_path = safe_cache_path(epub_cache, ebook_filename)
    if cached_path and cached_path.exists():
         return container.ebook_parser().get_kosync_id(cached_path)

    # [NEW] On-Demand Fetching
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
                        return kosync_id
        except Exception as e:
            logger.warning(f"⚠️ Failed to get KOSync ID from BookOrbit: {e}")

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
            logger.error(f"   ❌ Failed ABS on-demand download: {e}")

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
            logger.error(f"   ❌ Failed CWA on-demand download: {e}")

    # Neither source available - log helpful warning
    if (not clients.booklore_client.is_configured()
            and not clients.bookorbit_client.is_configured()
            and not EBOOK_DIR.exists()):
        logger.warning(
            f"⚠️ Cannot compute KOSync ID for '{ebook_filename}': "
            "No ebook source configured. Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, "
            "BOOKLORE_PASSWORD), enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, "
            "BOOKORBIT_PASSWORD), or mount the ebooks directory to /books"
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
    is_bookorbit = (ebook_source or "").strip().lower() == "bookorbit"
    if is_bookorbit:
        client = clients.bookorbit_client
        kobo_shelf = (user_setting("BOOKORBIT_SHELF_NAME") or "Kobo").strip()
        watch_enabled = str(os.environ.get("BOOKORBIT_SHELF_WATCH_ENABLED", "false")).strip().lower() in (
            "true", "1", "yes", "on"
        )
        watch_shelf = (os.environ.get("BOOKORBIT_SHELF_WATCH_NAME") or "Up Next").strip()
    else:
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
    use_id = is_bookorbit and ebook_source_id and hasattr(client, "add_book_id_to_shelf")
    try:
        if use_id:
            client.add_book_id_to_shelf(ebook_source_id, kobo_shelf)
        else:
            client.add_to_shelf(shelf_filename, kobo_shelf)
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to add '{sanitize_log_data(shelf_filename)}' to '{kobo_shelf}': {e}"
        )

    if watch_enabled and watch_shelf and watch_shelf != kobo_shelf:
        try:
            if use_id:
                client.remove_book_id_from_shelf(ebook_source_id, watch_shelf)
            else:
                client.remove_from_shelf(shelf_filename, watch_shelf)
        except Exception as e:
            logger.warning(
                f"⚠️ Failed to remove '{sanitize_log_data(shelf_filename)}' from watch shelf '{watch_shelf}': {e}"
            )


def _download_storyteller_artifact(storyteller_uuid, abs_title=None, *, original_ebook_filename=None):
    """Resolve a Storyteller artifact path.

    When ``STORYTELLER_NO_EPUB_CACHE`` is enabled and an original EPUB can be
    located via ``EbookParser.resolve_book_path``, skip the API download and
    return ``(original_name, original_path)``. Otherwise, download the
    Storyteller ReadAloud EPUB into the epub cache as before, falling back to
    a local ``STORYTELLER_LIBRARY_DIR`` copy on failure.

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
        downloaded = uc().storyteller_client.download_book(storyteller_uuid, target_path)
    except Exception as dl_err:
        logger.warning(f"Storyteller API download failed for '{storyteller_uuid}': {dl_err}")

    if downloaded:
        return artifact_filename, target_path

    st_lib = Path(os.environ.get("STORYTELLER_LIBRARY_DIR", "/storyteller_library"))
    if abs_title and st_lib.exists():
        for child in st_lib.iterdir():
            if not child.is_dir():
                continue
            readaloud = list(child.glob("*readaloud*.epub")) + list(child.glob("*synced*/*.epub"))
            if readaloud and child.name.lower().strip() == abs_title.lower().strip():
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
        logger.warning(f"Failed ABS chapter lookup for storyteller ingest '{book.abs_id}': {abs_err}")
        return []
    if not item_details:
        return []
    return item_details.get("media", {}).get("chapters", []) or []


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
        kosync_doc_id = get_kosync_id_for_ebook(resolved_ebook_filename, booklore_id)
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

    target_book.abs_title = abs_title or target_book.abs_title or Path(resolved_ebook_filename).stem
    target_book.ebook_filename = resolved_ebook_filename
    target_book.kosync_doc_id = kosync_doc_id
    target_book.status = "pending"
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
            )

    if selected_storyteller_uuid and uc().storyteller_client.is_configured():
        try:
            uc().storyteller_client.add_to_collection_by_uuid(selected_storyteller_uuid)
        except Exception as st_err:
            logger.warning(f"Failed to add Storyteller UUID to collection: {st_err}")

    shelf_filename = saved_book.original_ebook_filename or saved_book.ebook_filename
    if (
        shelf_filename
        and not _is_storyteller_artifact_filename(shelf_filename)
        and uc().booklore_client.is_configured()
    ):
        try:
            uc().booklore_client.add_to_shelf(shelf_filename)
        except Exception as bl_err:
            logger.warning(f"Failed to add Grimmory shelf entry for '{shelf_filename}': {bl_err}")

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

    return saved_book, None, None


class EbookResult:
    """Wrapper to provide consistent interface for ebooks from Grimmory, CWA, ABS, or filesystem."""

    def __init__(self, name, title=None, subtitle=None, authors=None, booklore_id=None, path=None, source=None, source_id=None, abs_identifier=None):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
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
            logger.warning(f"⚠️ Audiobook search failed for {source_name}: {e}")
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
        records.append(
            {
                "bridge_key": bridge_key,
                "audio_source": audio_source,
                "audio_source_id": source_id,
                "audio_title": title,
                "audio_author": author,
                "audio_duration": item.duration,
                "audio_cover_url": item.cover_url or "",
                "audio_path": item.path or "",
                "audio_provider_book_id": str(item.provider_book_id or source_id),
                "audio_provider_file_id": str(item.provider_file_id or ""),
                # Legacy aliases maintained for compatibility with existing templates/session keys.
                "id": bridge_key,
                "title": title,
                "authors": author,
                "duration": item.duration,
                "cover_url": item.cover_url or "",
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
        logger.warning(f"⚠️ Failed to build local ebook title index: {e}")
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
                            subtitle=b.get('subtitle'),
                            authors=b.get('authors'),
                            booklore_id=b.get('id'),
                            path=b.get('filePath') or b.get('filepath') or b.get('path'),
                            source='Grimmory'
                        ))
        except Exception as e:
            logger.warning(f"⚠️ Grimmory search failed: {e}")

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
                    path=b.get('filePath') or b.get('filepath') or b.get('path'),
                    source='BookOrbit',
                    source_id=b.get('id'),
                ))
        except Exception as e:
            logger.warning(f"⚠️ BookOrbit search failed: {e}")

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
                    path=None,
                    source='BookFusion',
                    source_id=bf_id,
                ))
        except Exception as e:
            logger.warning(f"⚠️ BookFusion search failed: {e}")

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
                                    source='ABS',
                                    source_id=ab.get('id')
                                ))
                                found_filenames.add(fname.lower())
                                if ab.get('title'):
                                    found_stems.add(ab['title'].lower().strip())
        except Exception as e:
            logger.warning(f"⚠️ ABS ebook search failed: {e}")

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
                                path=cr.get('download_url'),
                                source='CWA',
                                source_id=cwa_id,
                                abs_identifier=abs_identifier,
                            ))
                            found_filenames.add(fname.lower())
                            if cr.get('title'):
                                found_stems.add(cr['title'].lower().strip())
        except Exception as e:
            logger.warning(f"⚠️ CWA search failed: {e}")

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
            logger.warning(f"⚠️ Filesystem search failed: {e}")

    # Check if we have no sources at all
    if (not results and not EBOOK_DIR.exists()
            and not clients.booklore_client.is_configured()
            and not clients.bookorbit_client.is_configured()
            and not clients.bookfusion_client.is_configured()):
        logger.warning(
            "⚠️ No ebooks available: No ebook source configured. "
            "Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD), "
            "enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, BOOKORBIT_PASSWORD), "
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
    source_text = str(raw_source or "").strip()
    if not source_text:
        return ""
    source_map = {
        "booklore": "Booklore",
        "grimmory": "Booklore",
        "bookorbit": "BookOrbit",
        "bookfusion": "BookFusion",
        "abs": "ABS",
        "cwa": "CWA",
        "local file": "Local File",
    }
    return source_map.get(source_text.lower(), source_text)


def _build_forge_text_item(source_type, source_id, source_path, original_filename):
    normalized_source = _normalize_text_source_type(source_type)
    normalized_source_id = str(source_id or "").strip()
    normalized_source_path = str(source_path or "").strip()

    text_item = {
        "source": normalized_source,
        "path": normalized_source_path,
        "booklore_id": normalized_source_id,
        "bookorbit_id": normalized_source_id,
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
    if normalized_source == "BookFusion":
        text_item["bookfusion_id"] = normalized_source_id
    if normalized_source == "CWA":
        text_item["cwa_id"] = normalized_source_id
        if normalized_source_path:
            text_item["download_url"] = normalized_source_path
    if normalized_source == "Local File":
        text_item["path"] = normalized_source_path

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
    if ebook_source == "BookLore":
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
    target_book = existing_book or Book(abs_id=bridge_key, sync_mode="audiobook")
    target_book.abs_id = bridge_key
    target_book.abs_title = audio_title or target_book.abs_title or bridge_key
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
    target_book.status = "pending"
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

    saved_book = database_service.save_book(target_book)

    if uc().storyteller_client.is_configured() and saved_book.storyteller_uuid:
        try:
            uc().storyteller_client.add_to_collection_by_uuid(saved_book.storyteller_uuid)
        except Exception as st_err:
            logger.warning(f"Failed to add Storyteller UUID to collection: {st_err}")

    shelf_filename = saved_book.original_ebook_filename or saved_book.ebook_filename
    if shelf_filename and not _is_storyteller_artifact_filename(shelf_filename):
        try:
            _shelve_matched_ebook(
                shelf_filename,
                ebook_source=saved_book.ebook_source,
                ebook_source_id=saved_book.ebook_source_id,
            )
        except Exception as shelf_err:
            logger.warning(f"Failed to shelve matched ebook '{shelf_filename}': {shelf_err}")

    database_service.dismiss_suggestion(saved_book.abs_id)
    if isinstance(saved_book.kosync_doc_id, str) and saved_book.kosync_doc_id.strip():
        database_service.dismiss_suggestion(saved_book.kosync_doc_id)

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
        logger.warning("⚠️ BookFusion download failed for '%s': %s", bf_id, e)
        return None
    if not content:
        logger.warning("⚠️ BookFusion download returned no content for '%s'", bf_id)
        return None
    try:
        cached_path.write_bytes(content)
    except Exception as e:
        logger.error("❌ Could not cache BookFusion EPUB for '%s': %s", bf_id, e)
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
        logger.warning("⚠️ Could not compute KOSync hash for BookFusion '%s': %s", bookfusion_id, e)
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
    target_book.status = "pending"

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
        try:
            database_service.ensure_linked_kosync_document(saved_book.kosync_doc_id, saved_book.abs_id)
        except Exception as e:
            logger.warning(
                "⚠️ Could not link BookFusion KOSync document for '%s': %s", saved_book.abs_id, e
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
            saved_book.abs_id, e,
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
            'KOREADER_ANNOTATION_SYNC',
            'SYNC_FRESHNESS_GUARDS',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'BOOKFUSION_ENABLED',
            'GRIMMORY_READING_SESSIONS',
            'CWA_ENABLED',
            'CWA_SYNC_ENABLED',
            'HARDCOVER_ENABLED',
            'HARDCOVER_ANNOTATION_SYNC',
            'STORYGRAPH_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED',
            'ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID',
            'REPROCESS_ON_CLEAR_IF_NO_ALIGNMENT',
            'INSTANT_SYNC_ENABLED',
            'STORYTELLER_POLL_WAIT_FOR_SETTLE',
            'STORYTELLER_LISTENING_SESSIONS',
            'STORYTELLER_NO_EPUB_CACHE',
            'BOOKLORE_SHELF_WATCH_ENABLED',
            'BOOKORBIT_ENABLED',
            'BOOKORBIT_READING_SESSIONS',
            'BOOKORBIT_SHELF_WATCH_ENABLED',
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
            'SHELFMARK_URL', 'ABS_SERVER', 'BOOKLORE_SERVER', 'BOOKFUSION_API_URL',
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
            logger.error(f"❌ Error saving settings: {e}")

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

    response = make_response(render_template('settings.html',
                         message=message,
                         is_error=is_error,
                         users=users,
                         current_user_id=(cu.id if cu else None),
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
    if not isinstance(metadata, dict):
        return None, None
    series_list = metadata.get("series") or []
    if not isinstance(series_list, list) or not series_list:
        name = (metadata.get("seriesName") or "").strip()
        return (name or None, None)
    first = series_list[0]
    if isinstance(first, dict):
        name = (first.get("name") or "").strip() or None
        raw_seq = first.get("sequence")
    else:
        name = str(first).strip() or None
        raw_seq = None
    sequence = None
    if raw_seq is not None:
        try:
            sequence = float(raw_seq)
        except (TypeError, ValueError):
            sequence = None
    return name, sequence


def _extract_series_from_booklore_metadata(raw: dict) -> tuple:
    """Return (series_name, series_sequence) from cached BookLore raw_metadata."""
    if not isinstance(raw, dict):
        return None, None
    metadata = raw.get("metadata") or raw
    name = (metadata.get("seriesName") or "").strip() or None
    raw_seq = metadata.get("seriesNumber") or metadata.get("seriesSequence")
    sequence = None
    if raw_seq is not None:
        try:
            sequence = float(raw_seq)
        except (TypeError, ValueError):
            sequence = None
    return name, sequence


def _normalize_series_key(name: str) -> str:
    """Case- and whitespace-insensitive key for grouping series."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


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
    next_book = next((c for c in children if (c.get("unified_progress") or 0) < 100), None)

    last_sync_unix = 0.0
    for c in children:
        ts = c.get("last_sync_unix") or 0.0
        if ts > last_sync_unix:
            last_sync_unix = ts

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
        "last_sync_unix": last_sync_unix,
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

    if integrations.get('abs') and mapping.get('sync_mode') != 'ebook_only':
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


def _compute_dashboard_sync_warning_pct(mapping, integrations):
    progress_values = []
    states = mapping.get('states', {})

    for client_name in _get_dashboard_sync_warning_clients(mapping, integrations):
        state = states.get(client_name)
        if not state:
            continue
        raw = state.get('percentage')
        if raw is None or raw <= 0:
            continue
        value = _dashboard_text_axis_pct(client_name, state, mapping)
        if value is None:
            continue
        progress_values.append(value)

    if len(progress_values) < 2:
        return 0.0

    return round(max(progress_values) - min(progress_values), 1)


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


def _build_dashboard_mapping(
    book,
    states_by_book,
    integrations,
    hardcover_by_book,
    storygraph_by_book,
    reading_stats_by_book,
    cached_booklore_by_filename,
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
        "ebook_source": getattr(book, "ebook_source", None),
        "ebook_source_id": getattr(book, "ebook_source_id", None),
        "kosync_doc_id": book.kosync_doc_id,
        "transcript_file": book.transcript_file,
        "status": book.status,
        "sync_mode": getattr(book, "sync_mode", "audiobook"),
        "unified_progress": 0,
        "duration": book.duration or 0,
        "storyteller_uuid": book.storyteller_uuid,
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

    bookfusion_link = None
    try:
        user = current_user()
    except RuntimeError:
        user = None
    try:
        if user is not None:
            bookfusion_link = database_service.get_user_bookfusion_link(user.id, book.abs_id)
        elif current_app.config.get('LOGIN_DISABLED'):
            uid = database_service._default_user_id()
            bookfusion_link = database_service.get_user_bookfusion_link(uid, book.abs_id) if uid else None
    except Exception as exc:
        logger.debug("BookFusion dashboard link lookup failed for '%s': %s", book.abs_id, exc)
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
        mapping["audio_url"] = f"{manager.booklore_client.base_url}/book/{mapping['audio_source_id']}?tab=view"
    elif mapping["audio_source"] == "BookOrbit":
        mapping["abs_url"] = None
        _bo_audio_base = (os.environ.get("BOOKORBIT_SERVER") or "").rstrip("/")
        mapping["audio_url"] = f"{_bo_audio_base}/book/{mapping['audio_source_id']}" if _bo_audio_base else None
    else:
        mapping["abs_url"] = f"{manager.abs_client.base_url}/item/{book.abs_id}"
        mapping["audio_url"] = mapping["abs_url"]

    mapping["booklore_id"] = _get_cached_booklore_id(book, cached_booklore_by_filename=cached_booklore_by_filename)
    if manager.booklore_client.is_configured() and mapping["booklore_id"]:
        mapping["booklore_url"] = f"{manager.booklore_client.base_url}/book/{mapping['booklore_id']}?tab=view"
    else:
        mapping["booklore_url"] = None

    # BookOrbit deep links — frontend book route is /book/:bookId.
    _bo_base = (os.environ.get("BOOKORBIT_SERVER") or "").rstrip("/")
    if _bo_base and mapping.get("ebook_source") == "BookOrbit" and mapping.get("ebook_source_id"):
        mapping["bookorbit_url"] = f"{_bo_base}/book/{mapping['ebook_source_id']}"
    else:
        mapping["bookorbit_url"] = None
    if _bo_base and mapping.get("audio_source") == "BookOrbit" and mapping.get("audio_source_id"):
        mapping["bookorbit_audio_url"] = f"{_bo_base}/book/{mapping['audio_source_id']}"
    else:
        mapping["bookorbit_audio_url"] = None

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

    if mapping.get("audio_cover_url"):
        mapping["cover_url"] = mapping["audio_cover_url"]
    elif mapping.get("audio_source") == "BookLore" and mapping.get("audio_source_id"):
        mapping["cover_url"] = f"/api/booklore/audiobook-cover/{mapping['audio_source_id']}"
    elif mapping.get("audio_source") == "BookOrbit" and mapping.get("audio_source_id"):
        mapping["cover_url"] = f"/api/bookorbit/audiobook-cover/{mapping['audio_source_id']}"
    elif book.abs_id and mapping.get("audio_source") not in _LIBRARY_AUDIO_SOURCES:
        mapping["cover_url"] = f"{manager.abs_client.base_url}/api/items/{book.abs_id}/cover?token={manager.abs_client.token}"

    reading_stats = reading_stats_by_book.get(book.abs_id)
    if reading_stats:
        mapping["reading_stats"] = reading_stats

    return mapping


def _build_dashboard_mappings(
    books,
    all_states,
    integrations,
    all_hardcover=None,
    all_storygraph=None,
    reading_stats_by_book=None,
    cached_booklore_by_filename=None,
):
    hardcover_by_book = {h.abs_id: h for h in (all_hardcover or [])}
    storygraph_by_book = {s.abs_id: s for s in (all_storygraph or [])}
    states_by_book = _group_dashboard_states_by_book(all_states)
    reading_stats_by_book = reading_stats_by_book or {}
    cached_booklore_by_filename = cached_booklore_by_filename or {}

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
        )
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
    # FIX: Enforce minimum length to prevent short/empty matches (e.g. "The", "It", "")
    MIN_LEN = 4
    
    if len(title) >= MIN_LEN and title in search_norm: return True
    if len(author) >= MIN_LEN and author in search_norm: return True

    return False

# ---------------- ROUTES ----------------
def index():
    """Dashboard - loads books and progress from database service"""
    user = current_user()
    user_id = user.id if user else None
    books = database_service.get_all_books()
    all_states = database_service.get_all_states(
        user_id=user_id
    )
    books = _dashboard_visible_books_for_user(books, user)
    all_hardcover = database_service.get_all_hardcover_details()
    all_storygraph = database_service.get_all_storygraph_details()
    all_reading_stats = database_service.get_all_reading_stats(user_id=user_id)
    cached_booklore_by_filename = _index_cached_booklore_books(database_service.get_all_booklore_books())
    integrations = _build_dashboard_integrations()
    mappings, overall_progress = _build_dashboard_mappings(
        books,
        all_states,
        integrations,
        all_hardcover=all_hardcover,
        all_storygraph=all_storygraph,
        reading_stats_by_book=all_reading_stats,
        cached_booklore_by_filename=cached_booklore_by_filename,
    )

    suggestions = []
    if current_app.config.get('LOGIN_DISABLED') or (user and getattr(user, "is_admin", False)):
        suggestions = [s for s in database_service.get_all_pending_suggestions() if len(s.matches) > 0]
    grouped_mappings = _group_dashboard_mappings_by_series(mappings)

    latest_version, update_available = get_update_status()

    return render_template(
        'index.html',
        mappings=mappings,
        grouped_mappings=grouped_mappings,
        integrations=integrations,
        progress=overall_progress,
        suggestions=suggestions,
        app_version=APP_VERSION,
        update_available=update_available,
        latest_version=latest_version
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
                logger.warning(f"⚠️ Forge audio Grimmory search failed: {e}")

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
                logger.warning(f"⚠️ Forge audio BookOrbit search failed: {e}")

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
            logger.warning(f"⚠️ Forge: Grimmory search failed: {e}")

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
        logger.warning(f"⚠️ Forge: BookOrbit search failed: {e}")

    # 2b. BookFusion
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
        logger.warning(f"⚠️ Forge: BookFusion search failed: {e}")

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
        logger.warning(f"⚠️ Forge: ABS ebook search failed: {e}")

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
        logger.warning(f"⚠️ Forge: CWA search failed: {e}")

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
        logger.warning(f"⚠️ Forge: Local file search failed: {e}")

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
    if audio_source == "ABS" and not requested_abs_id:
        return jsonify({"error": "Missing abs_id"}), 400
    if audio_source in _LIBRARY_AUDIO_SOURCES and not audio_source_id:
        return jsonify({"error": "Missing audio_source_id"}), 400

    abs_id = requested_abs_id if audio_source == "ABS" else _build_bridge_key(audio_source, audio_source_id)
    clients = uc()

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
        logger.warning(f"⚠️ Forge: Could not get audio metadata for '{abs_id}': {e}")

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
        logger.error(f"❌ Failed to start forge: {e}")
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
        return jsonify(database_service.get_alignment_provenance())
    except Exception as e:
        logger.error(f"❌ Failed to read alignment provenance: {e}")
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
        logger.error(f"❌ Failed to queue re-align: {e}")
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
            
        # [NEW] Storyteller Tri-Link Logic
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
                logger.error(f"❌ Storyteller Link failed: {e}")
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
        book = Book(
            abs_id=abs_id,
            abs_title=abs_title,
            audio_source="ABS",
            audio_source_id=abs_id,
            audio_title=abs_title,
            audio_cover_url=f"{clients.abs_client.base_url}/api/items/{abs_id}/cover?token={clients.abs_client.token}",
            audio_duration=manager.get_duration(selected_ab),
            audio_provider_book_id=abs_id,
            ebook_filename=ebook_filename,
            kosync_doc_id=kosync_doc_id,
            transcript_file=transcript_file,
            status="pending",
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
                logger.error(f"❌ Failed to merge book data: {e}")

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
        
        # [NEW] Robust Dismissal: Check if there's a different hash for this filename (e.g. from device)
        try:
            device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
            if device_doc and device_doc.document_hash != kosync_doc_id:
                logger.info(f"🔄 Dismissing additional suggestion/hash for '{ebook_filename}': '{device_doc.document_hash}'")
                database_service.dismiss_suggestion(device_doc.document_hash)
        except Exception as e:
            logger.warning(f"⚠️ Failed to check/dismiss device hash: {e}")

        return redirect(url_for('index'))

    search = request.args.get('search', '').strip().lower()
    audiobooks, ebooks, storyteller_books = [], [], []
    if search:
        audiobooks = _search_audiobooks_with_fallback(search)

        # Use new search method
        ebooks = _search_ebooks_with_fallback(search)
        ebooks = _promote_authoritative_ebook_matches(audiobooks, ebooks)

        # Search Storyteller
        if uc().storyteller_client.is_configured():
            try:
                storyteller_books = uc().storyteller_client.search_books(search)
            except Exception as e:
                logger.warning(f"⚠️ Storyteller search failed in match route: {e}")

    return render_template('match.html', audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books, search=search)


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
        logger.warning("Forge & Match: failed to record job state for '%s': %s", sanitize_log_data(abs_id), exc)
        return None


def _process_forge_only_queue(queue_items, forge_stage_mode=None):
    """Background processor for the Add Book 'Forge only' action.

    Builds the Storyteller edition for each forge-eligible item (audio + a standard
    ebook) without creating a sync mapping — the same work as forge_process(), driven
    from queued items instead of a single JSON request.
    """
    clients = uc()
    for item in queue_items:
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
            logger.warning("Forge only: metadata lookup failed for '%s': %s", sanitize_log_data(abs_id), e)

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
            logger.error("❌ Forge only failed for '%s': %s", sanitize_log_data(title), e)


def _process_batch_queue(queue_items):
    """Background processor for the batch-match 'Process Queue' action.

    Runs each item's onboarding (artifact download, hash, transcript ingest, save,
    collection/shelf) off the request thread via _spawn_user_background, so the page
    redirects immediately and books appear on the dashboard as each finishes.
    """
    from src.db.models import Book
    clients = uc()
    for item in queue_items:
        if not item.get('audio_source'):
            # No audio: ebook-only / storyteller-only item — match only.
            _create_ebook_only_mapping_from_queue_item(item)
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
                logger.error(f"❌ Storyteller Tri-Link failed for '{item['abs_title']}': {e}")
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
        if shelf_filename:
            _shelve_matched_ebook(shelf_filename, item.get('ebook_source'),
                                  item.get('ebook_source_id'))
        if clients.storyteller_client.is_configured():
            if book.storyteller_uuid:
                clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

        # Auto-dismiss any pending suggestion
        database_service.dismiss_suggestion(item['abs_id'])
        database_service.dismiss_suggestion(kosync_doc_id)

        # [NEW] Robust Dismissal
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
        if not item.get('audio_source'):
            # No audio: ebook-only / storyteller-only item — match only (nothing to forge).
            _create_ebook_only_mapping_from_queue_item(item)
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
            if clients.booklore_client.is_configured():
                shelf_filename = original_ebook_filename or ebook_filename
                _shelve_matched_ebook(shelf_filename)
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

        if source_type in ('ABS', 'Booklore', 'BookOrbit', 'CWA') and not source_id:
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

        database_service.dismiss_suggestion(forge_id)
        if kosync_doc_id:
            database_service.dismiss_suggestion(kosync_doc_id)



def _add_book_view(template_name, self_endpoint):
    """Shared queue-based Add Book / Batch Match view.

    `batch_match()` (legacy `/batch-match`) and `add_book()` (`/add-book`, the unified
    Add Book page) both delegate here; they differ only in which template renders and
    which endpoint the in-page queue actions redirect back to.
    """
    clients = uc()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_to_queue':
            abs_id = request.form.get('audiobook_id')
            audio_source = (request.form.get('audio_source') or ('ABS' if abs_id else '')).strip() or None
            audio_source_id = (request.form.get('audio_source_id') or abs_id or '').strip() or None
            audio_title = (request.form.get('audio_title') or '').strip() or None
            audio_cover_url = (request.form.get('audio_cover_url') or '').strip() or None
            audio_provider_book_id = (request.form.get('audio_provider_book_id') or audio_source_id or '').strip() or None
            audio_provider_file_id = (request.form.get('audio_provider_file_id') or '').strip() or None
            audio_duration = _parse_audio_duration(request.form.get('audio_duration'))
            ebook_filename = request.form.get('ebook_filename', '')
            ebook_display_name = request.form.get('ebook_display_name', ebook_filename)
            ebook_source = (request.form.get('ebook_source') or request.form.get('source_type') or '').strip() or None
            ebook_source_id = (request.form.get('ebook_source_id') or request.form.get('source_id') or '').strip() or None
            ebook_source_path = (request.form.get('ebook_source_path') or request.form.get('source_path') or '').strip() or None
            storyteller_uuid = request.form.get('storyteller_uuid', '')
            audiobooks = get_audiobooks_conditionally()
            selected_ab = next((ab for ab in audiobooks if ab['id'] == abs_id), None)
            selected_audio = None
            if audio_source == 'ABS' and selected_ab:
                selected_audio = {
                    'bridge_key': abs_id,
                    'audio_source': 'ABS',
                    'audio_source_id': abs_id,
                    'audio_title': manager.get_abs_title(selected_ab),
                    'audio_duration': manager.get_duration(selected_ab),
                    'audio_cover_url': f"{clients.abs_client.base_url}/api/items/{abs_id}/cover?token={clients.abs_client.token}",
                    'audio_provider_book_id': abs_id,
                    'audio_provider_file_id': None,
                }
            elif audio_source in _LIBRARY_AUDIO_SOURCES and audio_source_id:
                selected_audio = {
                    'bridge_key': _build_bridge_key(audio_source, audio_source_id),
                    'audio_source': audio_source,
                    'audio_source_id': audio_source_id,
                    'audio_title': audio_title or f"{_audio_source_display_name(audio_source)} {audio_source_id}",
                    'audio_duration': audio_duration,
                    'audio_cover_url': audio_cover_url,
                    'audio_provider_book_id': audio_provider_book_id,
                    'audio_provider_file_id': audio_provider_file_id,
                }
            elif not audio_source and (ebook_filename or storyteller_uuid):
                # No audio selected: ebook-only / storyteller-only item (match only).
                _eb_key = (storyteller_uuid or ebook_filename or '').strip()
                if _eb_key:
                    selected_audio = {
                        'bridge_key': f"ebook:{_eb_key}",
                        'audio_source': None,
                        'audio_source_id': None,
                        'audio_title': ebook_display_name or (Path(ebook_filename).stem if ebook_filename else 'Ebook'),
                        'audio_duration': None,
                        'audio_cover_url': None,
                        'audio_provider_book_id': None,
                        'audio_provider_file_id': None,
                    }

            if selected_audio and (ebook_filename or storyteller_uuid):
                _match_queue_add({
                    **selected_audio,
                    "abs_id": selected_audio['bridge_key'],
                    "abs_title": selected_audio['audio_title'],
                    "ebook_filename": ebook_filename,
                    "ebook_display_name": ebook_display_name,
                    "ebook_source": ebook_source,
                    "ebook_source_id": ebook_source_id,
                    "ebook_source_path": ebook_source_path,
                    "storyteller_uuid": storyteller_uuid,
                    "duration": selected_audio['audio_duration'],
                    "cover_url": selected_audio['audio_cover_url'],
                })
            # Clear the search box + results after queueing so the user starts the
            # next book from a clean search (drop the preserved `search` term).
            return redirect(url_for(self_endpoint))
        elif action == 'remove_from_queue':
            abs_id = request.form.get('abs_id')
            _match_queue_remove(abs_id)
            return redirect(url_for(self_endpoint))
        elif action == 'clear_queue':
            _match_queue_clear()
            return redirect(url_for(self_endpoint))
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
        if clients.storyteller_client.is_configured():
            try:
                storyteller_books = clients.storyteller_client.search_books(search)
            except Exception as e:
                logger.warning(f"⚠️ Storyteller search failed in batch_match route: {e}")

    return render_template(template_name, audiobooks=audiobooks, ebooks=ebooks, storyteller_books=storyteller_books,
                           queue=_load_match_queue(), search=search, self_endpoint=self_endpoint)


def batch_match():
    """Legacy `/batch-match` route; GET folds into the unified Add Book page."""
    if request.method == 'GET':
        search = request.args.get('search', '')
        return redirect(url_for('add_book', search=search), code=302)
    return _add_book_view('batch_match.html', 'batch_match')


def add_book():
    """Unified `/add-book` page — single, batch, forge, and match in one queue-based flow."""
    return _add_book_view('add_book.html', 'add_book')


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
            logger.warning(f"Could not read suggestions cache file '{cache_file}': {e}")
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
            logger.warning(f"Could not persist suggestions cache file '{cache_file}': {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass


def _match_queue_file_path():
    return DATA_DIR / MATCH_QUEUE_FILE_NAME


def _read_match_queue_unlocked() -> list:
    queue_file = _match_queue_file_path()
    if not queue_file.exists():
        return []
    try:
        raw = json.loads(queue_file.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f"Could not read match queue file '{queue_file}': {e}")
        return []
    items = raw.get('items', []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _write_match_queue_unlocked(items: list) -> None:
    queue_file = _match_queue_file_path()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": items if isinstance(items, list) else [], "updated_at": time.time()}
    temp_file = queue_file.with_suffix('.tmp')
    try:
        temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temp_file.replace(queue_file)
    except Exception as e:
        logger.warning(f"Could not persist match queue file '{queue_file}': {e}")
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass


def _load_match_queue() -> list:
    """Return the persisted batch-match queue items."""
    with MATCH_QUEUE_LOCK:
        return _read_match_queue_unlocked()


def _save_match_queue(items: list) -> None:
    """Replace the persisted batch-match queue with the given items."""
    with MATCH_QUEUE_LOCK:
        _write_match_queue_unlocked(items)


def _match_queue_add(item: dict) -> bool:
    """Append an item unless its bridge_key is already queued. Returns True if added."""
    with MATCH_QUEUE_LOCK:
        items = _read_match_queue_unlocked()
        bridge_key = item.get('bridge_key')
        if bridge_key and any(existing.get('bridge_key') == bridge_key for existing in items):
            return False
        items.append(item)
        _write_match_queue_unlocked(items)
        return True


def _match_queue_remove(abs_id) -> None:
    with MATCH_QUEUE_LOCK:
        items = [item for item in _read_match_queue_unlocked() if item.get('abs_id') != abs_id]
        _write_match_queue_unlocked(items)


def _match_queue_clear() -> None:
    with MATCH_QUEUE_LOCK:
        _write_match_queue_unlocked([])


def _match_queue_drain() -> list:
    """Return all queued items and clear the queue atomically."""
    with MATCH_QUEUE_LOCK:
        items = _read_match_queue_unlocked()
        _write_match_queue_unlocked([])
        return items


def _match_queue_response():
    """Re-render the queue panel fragment for an XHR add/remove/clear (so the page
    isn't reloaded and scroll position is preserved); otherwise redirect to /suggestions."""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('_match_queue_panel.html', queue=_load_match_queue())
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
    state_id, suggestions_state = _get_suggestions_state(create=True)
    if suggestions_state is None:
        suggestions_state = _default_suggestions_state()

    if request.method == 'POST':
        action = request.form.get('action')

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
            bridge_key = (request.form.get('audiobook_id') or '').strip()
            audio_source = (
                request.form.get('audio_source')
                or _audio_source_from_bridge_key(bridge_key)
            ).strip() or None
            audio_source_id = (request.form.get('audio_source_id') or bridge_key).strip() or None
            audio_title = (request.form.get('audio_title') or '').strip() or None
            audio_cover_url = (request.form.get('audio_cover_url') or '').strip() or None
            audio_provider_book_id = (request.form.get('audio_provider_book_id') or audio_source_id or '').strip() or None
            audio_provider_file_id = (request.form.get('audio_provider_file_id') or '').strip() or None
            audio_duration = _parse_audio_duration(request.form.get('audio_duration'))
            ebook_filename = request.form.get('ebook_filename', '')
            ebook_display_name = request.form.get('ebook_display_name', ebook_filename)
            ebook_source = (request.form.get('ebook_source') or '').strip() or None
            ebook_source_id = (request.form.get('ebook_source_id') or '').strip() or None
            ebook_source_path = (request.form.get('ebook_source_path') or request.form.get('source_path') or '').strip() or None
            storyteller_uuid = request.form.get('storyteller_uuid', '')
            selected_audio = None
            if audio_source == 'ABS' and audio_source_id:
                selected_ab = None
                if not audio_title or audio_duration is None:
                    abs_client = uc().abs_client
                    abs_items = abs_client.get_all_audiobooks()
                    selected_ab = next((ab for ab in abs_items if str(ab.get('id')) == audio_source_id), None)
                else:
                    abs_client = uc().abs_client

                resolved_title = audio_title or (manager.get_abs_title(selected_ab) if selected_ab else '') or audio_source_id
                resolved_duration = audio_duration if audio_duration is not None else (
                    manager.get_duration(selected_ab) if selected_ab else None
                )
                resolved_cover = (
                    audio_cover_url
                    or f"{abs_client.base_url}/api/items/{audio_source_id}/cover?token={abs_client.token}"
                )
                selected_audio = {
                    'bridge_key': bridge_key or audio_source_id,
                    'audio_source': 'ABS',
                    'audio_source_id': audio_source_id,
                    'audio_title': resolved_title,
                    'audio_duration': resolved_duration,
                    'audio_cover_url': resolved_cover,
                    'audio_provider_book_id': audio_provider_book_id or audio_source_id,
                    'audio_provider_file_id': audio_provider_file_id,
                }
            elif audio_source in _LIBRARY_AUDIO_SOURCES and audio_source_id:
                selected_audio = {
                    'bridge_key': bridge_key or _build_bridge_key(audio_source, audio_source_id),
                    'audio_source': audio_source,
                    'audio_source_id': audio_source_id,
                    'audio_title': audio_title or f"{_audio_source_display_name(audio_source)} {audio_source_id}",
                    'audio_duration': audio_duration,
                    'audio_cover_url': audio_cover_url,
                    'audio_provider_book_id': audio_provider_book_id,
                    'audio_provider_file_id': audio_provider_file_id,
                }

            if selected_audio and (ebook_filename or storyteller_uuid):
                _match_queue_add({
                    **selected_audio,
                    "abs_id": selected_audio['bridge_key'],
                    "abs_title": selected_audio['audio_title'],
                    "ebook_filename": ebook_filename,
                    "ebook_display_name": ebook_display_name,
                    "ebook_source": ebook_source,
                    "ebook_source_id": ebook_source_id,
                    "ebook_source_path": ebook_source_path,
                    "storyteller_uuid": storyteller_uuid,
                    "duration": selected_audio['audio_duration'],
                    "cover_url": selected_audio['audio_cover_url'],
                })
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
            cache_by_abs = suggestions_state.get('scan_cache_by_abs', {}) or {}
            for key in keys:
                item = _queue_item_from_suggestion(cache_by_abs.get(key))
                if item:
                    _match_queue_add(item)
            return _match_queue_response()

        elif action == 'forge_and_match_queue':
            # Same forge/match-all path as the Add Book page: Storyteller items just match,
            # ebook-only items run the forge (transcribe + align) pipeline in the background.
            _queue_items = _match_queue_drain()
            _spawn_user_background(_process_forge_match_queue, _queue_items, label="batch-forge-match")
            flash(f"Forging + matching {len(_queue_items)} book(s) in the background…", "info")
            return redirect(url_for('index'))

        elif action == 'process_queue':
            from src.db.models import Book

            clients = uc()
            for item in _load_match_queue():
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
                            "Suggestions skipped BookFusion link for '%s': %s",
                            sanitize_log_data(item.get('audio_title') or item.get('abs_title') or item.get('abs_id')),
                            err_msg,
                        )
                    elif saved_book:
                        _claim_book_for_user_id(get_current_user_id(), saved_book.abs_id)
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
                            "Suggestions skipped %s audiobook '%s': %s",
                            _audio_source_display_name(audio_source),
                            sanitize_log_data(item.get('audio_title') or item.get('audio_source_id')),
                            err_msg,
                        )
                    elif saved_book:
                        # Approving a shelf-watch suggestion needs the Up Next
                        # leg of the shelf move; _create_or_update_library_audio_mapping
                        # only adds to Kobo, it does not remove from Up Next.
                        try:
                            sw_pending = database_service.get_pending_suggestion(item.get('audio_source_id') or saved_book.abs_id)
                        except Exception:
                            sw_pending = None
                        if (
                            sw_pending
                            and getattr(sw_pending, 'origin', None) == 'shelf_watch'
                        ):
                            try:
                                meta = sw_pending.origin_metadata or {}
                                lib_client, watch_shelf, _kobo = _shelf_watch_clients_for(meta)
                                grimmory_filename = meta.get('grimmory_filename')
                                if grimmory_filename and lib_client and lib_client.is_configured():
                                    lib_client.remove_from_shelf(grimmory_filename, watch_shelf)
                            except Exception as bl_err:
                                logger.warning(f"Shelf-watch approval Up Next removal failed: {bl_err}")
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
                        logger.info(f"Batch Match: Using Storyteller Artifact '{storyteller_uuid}' for '{item['abs_title']}'")

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
                                "Batch Match Tri-Link",
                            )
                        else:
                            logger.warning(f"Failed to obtain Storyteller artifact '{storyteller_uuid}' for '{item['abs_title']}', skipping")
                            continue
                    except Exception as e:
                        logger.error(f"Storyteller Tri-Link failed for '{item['abs_title']}': {e}")
                        continue
                else:
                    if clients.booklore_client.is_configured():
                        book = clients.booklore_client.find_book_by_filename(ebook_filename)
                        if book:
                            booklore_id = book.get('id')

                    kosync_doc_id = get_kosync_id_for_ebook(
                        ebook_filename,
                        booklore_id,
                        source_path=item.get('ebook_source_path'),
                    )

                if not kosync_doc_id:
                    logger.warning(f"Could not compute KOSync ID for {sanitize_log_data(ebook_filename)}, skipping")
                    continue

                current_book_entry = database_service.get_book(item['abs_id'])
                if current_book_entry and current_book_entry.kosync_doc_id:
                    logger.info(f"Preserving existing hash '{current_book_entry.kosync_doc_id}' for '{item['abs_id']}' instead of new hash '{kosync_doc_id}'")
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

                _enqueue_tracker_automatch(clients.sync_clients, book)

                if not str(item['abs_id']).startswith('booklore:'):
                    clients.abs_client.add_to_collection(item['abs_id'], user_setting("ABS_COLLECTION_NAME", "Synced with KOReader"))

                # If this suggestion originated from the shelf-watch flow, do a full
                # shelf MOVE (Up Next -> Kobo) rather than just an add. The origin
                # metadata carries the Grimmory filename which is the canonical key
                # for shelf operations even if the user picked a different ebook
                # source during approval.
                shelf_watch_pending = None
                try:
                    shelf_watch_pending = database_service.get_pending_suggestion(item['abs_id'])
                except Exception:
                    shelf_watch_pending = None
                if (
                    shelf_watch_pending
                    and getattr(shelf_watch_pending, 'origin', None) == 'shelf_watch'
                ):
                    try:
                        meta = shelf_watch_pending.origin_metadata or {}
                        lib_client, watch_shelf, kobo_shelf = _shelf_watch_clients_for(meta)
                        grimmory_filename = meta.get('grimmory_filename')
                        if grimmory_filename and lib_client and lib_client.is_configured():
                            lib_client.move_between_shelves(
                                grimmory_filename, watch_shelf, kobo_shelf,
                            )
                    except Exception as bl_err:
                        logger.warning(f"Shelf-watch approval move failed: {bl_err}")
                elif clients.booklore_client.is_configured():
                    shelf_filename = original_ebook_filename or ebook_filename
                    clients.booklore_client.add_to_shelf(shelf_filename, user_setting("BOOKLORE_SHELF_NAME", "Kobo"))
                if clients.storyteller_client.is_configured():
                    if book.storyteller_uuid:
                        clients.storyteller_client.add_to_collection_by_uuid(book.storyteller_uuid)

                database_service.dismiss_suggestion(item['abs_id'])
                database_service.dismiss_suggestion(kosync_doc_id)

                try:
                    device_doc = database_service.get_kosync_doc_by_filename(ebook_filename)
                    if device_doc and device_doc.document_hash != kosync_doc_id:
                        database_service.dismiss_suggestion(device_doc.document_hash)
                except Exception:
                    pass

            _match_queue_clear()
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
        suggestions=scan_results,
        queue=_load_match_queue(),
        scan_has_run=bool(suggestions_state.get('scan_has_run', False)),
        scan_in_progress=scan_in_progress,
        scan_error=scan_error,
        scan_stats=suggestions_state.get('scan_last_stats', {}),
        storyteller_enabled=bool(uc().storyteller_client.is_configured()),
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
            logger.warning(f"⚠️ Failed to delete audio cache: {e}")

    # Clean up full transcript directory (chapter JSON files + manifest)
    transcript_dir = DATA_DIR / "transcripts" / "storyteller" / book.abs_id
    if transcript_dir.exists():
        try:
            shutil.rmtree(transcript_dir)
            logger.info(f"🗑️ Deleted transcript directory: {transcript_dir}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete transcript directory: {e}")

    if book.ebook_filename:
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

            cached_path = safe_cache_path(cache_dir_path, book.ebook_filename)
            if cached_path and cached_path.exists():
                try:
                    cached_path.unlink()
                    logger.info(f"🗑️ Deleted cached ebook file: {book.ebook_filename}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete cached ebook {book.ebook_filename}: {e}")

    if getattr(book, 'sync_mode', 'audiobook') == 'ebook_only' and book.kosync_doc_id:
        logger.info(f"🗑️ Deleting KOSync document record for ebook-only mapping: '{book.kosync_doc_id}'")
        database_service.delete_kosync_document(book.kosync_doc_id)

    is_abs_backed = (
        getattr(book, 'sync_mode', 'audiobook') != 'ebook_only'
        and not str(book.abs_id).startswith('booklore:')
    )
    if is_abs_backed:
        collection_name = user_setting('ABS_COLLECTION_NAME', 'Synced with KOReader')
        try:
            clients.abs_client.remove_from_collection(book.abs_id, collection_name)
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove from ABS collection: {e}")
    else:
        logger.info(f"Skipping ABS collection cleanup for non-ABS mapping '{book.abs_id}'")

    storyteller_uuid = getattr(book, 'storyteller_uuid', None)
    if not storyteller_uuid and getattr(book, 'ebook_filename', None):
        match = re.match(r"^storyteller_([0-9a-fA-F-]+)\.epub$", book.ebook_filename)
        if match:
            storyteller_uuid = match.group(1)
            logger.info(f"Inferred Storyteller UUID for cleanup: '{storyteller_uuid[:8]}...'")

    if storyteller_uuid:
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
            logger.warning(f"Failed to remove from Storyteller collection: {e}")

    if book.ebook_filename:
        shelf_filename = book.original_ebook_filename or book.ebook_filename
        is_bookorbit = (getattr(book, 'ebook_source', None) or '').strip().lower() == 'bookorbit'
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
            else:
                client = clients.booklore_client
                if client.is_configured():
                    shelf_name = user_setting('BOOKLORE_SHELF_NAME', 'Kobo')
                    client.remove_from_shelf(shelf_filename, shelf_name)
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove from {'BookOrbit' if is_bookorbit else 'Grimmory'} shelf: {e}")


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
        logger.error(f"❌ Failed to clear progress for '{abs_id}': {e}")

    return redirect(url_for('index'))



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
        if client_name.lower() == 'abs':
            try:
                success = bool(client.abs_client.mark_finished(abs_id))
            except Exception as e:
                logger.error(f"❌ ABS mark_finished failed for '{abs_id}': {e}")
        else:
            try:
                result = client.update_progress(book, update_req)
                success = getattr(result, 'success', False) if result else False
            except Exception as e:
                logger.error(f"❌ '{client_name}' mark-complete failed for '{abs_id}': {e}")

        # Only persist state when the write succeeded.
        if success:
            # Preserve any locator metadata returned by the client (e.g. KoSync's
            # completion XPath) so the synced state has a viable locator.
            updated_state = {}
            if client_name.lower() != 'abs':
                updated_state = getattr(result, 'updated_state', {}) or {}
            state = State(
                abs_id=abs_id,
                client_name=client_name.lower(),
                percentage=1.0,
                timestamp=int(time.time()),
                last_updated=int(time.time()),
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

    old_hash = book.kosync_doc_id

    if new_hash:
        book.kosync_doc_id = new_hash
        database_service.save_book(book)
        logger.info(f"✅ Updated KoSync hash for '{sanitize_log_data(book.abs_title)}' to manual input: '{new_hash}'")
        updated = True
    else:
        # Auto-regenerate
        # [NEW] User Request: If recalculating (empty input), prioritize the standard EPUB (original_ebook_filename)
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
                f"for '{sanitize_log_data(book.abs_title)}': {e}"
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


def serve_cover(filename):
    """Serve cover images with lazy extraction."""
    # Filename is likely <hash>.jpg
    doc_hash = filename.replace('.jpg', '')

    # 1. Check if file exists
    cover_path = COVERS_DIR / filename
    if cover_path.exists():
        return send_from_directory(COVERS_DIR, filename)

    # 2. Try to extract
    # Find book by kosync ID
    book = database_service.get_book_by_kosync_id(doc_hash)

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
                 return send_from_directory(COVERS_DIR, filename)
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

    # [NEW] Handle explicit unlinking
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
                logger.warning(f"Failed to remove Storyteller UUID from collection: {e}")
        
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

        return jsonify({"message": "Book linked successfully", "filename": saved_book.ebook_filename}), 200
    except Exception as e:
        logger.error(f"❌ Error linking Storyteller book for '{abs_id}': {e}")
        return jsonify({"error": str(e)}), 500


def _get_stats_timezone():
    tz_name = os.environ.get("TZ", "America/New_York") or "America/New_York"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("Invalid TZ '%s' for stats, falling back to America/New_York", tz_name)
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
        logger.warning("Stats API: listening stats build failed: %s", e)
        listening = None

    try:
        reading = _build_reading_stats_payload(tz)
    except Exception as e:
        logger.warning("Stats API: reading stats build failed: %s", e)
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
        logger.warning("Stats API: reading day drilldown failed for %s: %s", date_str, e)
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
        logger.warning("Stats API: reading calendar failed for %s: %s", month_str, e)
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
        logger.warning("Stats API: book detail failed for %s: %s", key, e)
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
        logger.warning("Stats API: yearly recap failed for %s/%s: %s", scope, year, e)
        return jsonify({"error": "Failed to load yearly recap"}), 500


def api_status():
    """Return status of all books from database service"""
    user = current_user()
    user_id = user.id if user else None
    books = database_service.get_all_books()
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
        logger.error(f"❌ Error fetching logs: {e}")
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
        logger.error(f"❌ Error fetching live logs: {e}")
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
                    logger.warning(f"Failed to clean audio cache {entry.name}: {e}")

    transcript_root = DATA_DIR / "transcripts" / "storyteller"
    if transcript_root.exists():
        for entry in transcript_root.iterdir():
            if entry.is_dir() and entry.name not in active_ids:
                try:
                    shutil.rmtree(entry)
                    deleted_transcripts += 1
                    logger.info(f"Cleaned transcript dir: {entry.name}")
                except Exception as e:
                    logger.warning(f"Failed to clean transcript dir {entry.name}: {e}")

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
                    logger.warning(f"Failed to clean cached epub {entry.name}: {e}")

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
                        logger.warning(f"Could not extract text for storyteller backfill: {e}")
                        
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
            logger.warning(f"Storyteller backfill failed for '{abs_id}': {e}")

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
    """
    Heuristic: extract series name + sequence from a title like
    "Solar Dragons Need Love, Too! 2" or "Returner's Defiance 3".

    Handles patterns:
      "Series Name N"          → (Series Name, N)
      "Series Name, Book N"    → (Series Name, N)
      "Series Name (Book N)"   → (Series Name, N)
    Returns (None, None) when no clear numeric suffix is found.
    """
    if not title:
        return None, None
    # Strip trailing unabridged/abridged qualifiers
    clean = re.sub(r'\s*\((?:unabridged|abridged|audio(?:\s+book)?)\)\s*$', '', title.strip(), flags=re.IGNORECASE)

    # "Title, Book N" / "Title - Book N" / "Title (Book N)"
    m = re.search(
        r'^(.+?)[\s,\-:]+\(?(?:book|volume|vol\.?|part)\s+(\d+(?:\.\d+)?)\)?\s*$',
        clean, re.IGNORECASE,
    )
    if m:
        series = m.group(1).rstrip(' ,.!:-').strip()
        if series:
            return series, float(m.group(2))

    # "Title N" — trailing integer (not float, to avoid matching "Author 2.0")
    m = re.match(r'^(.+?)\s+(\d{1,3})\s*$', clean)
    if m:
        series = m.group(1).rstrip(' ,.!:-').strip()
        seq = int(m.group(2))
        # Guard: series candidate must be non-trivially long and seq plausible
        if len(series) >= 4 and 1 <= seq <= 50:
            return series, float(seq)

    return None, None


def api_series_backfill():
    """Backfill series_name/series_sequence for all books that lack it.

    Tries ABS metadata first; falls back to parsing the number out of the title.
    Writes via direct SQL UPDATE to avoid ORM session lifecycle issues.
    """
    import time as _time
    import sqlalchemy as _sa
    start = _time.time()
    db = container.database_service()
    abs_client = container.abs_client()
    if not abs_client or not abs_client.is_configured():
        return jsonify({"error": "ABS not configured"}), 400

    # Collect all rows that need updating — read-only pass
    with db.get_session() as session:
        rows = session.execute(
            _sa.text("SELECT abs_id, abs_title, audio_source FROM books WHERE series_name IS NULL OR series_name = ''")
        ).fetchall()

    updates = []   # list of (abs_id, series_name, series_sequence)
    skipped = 0
    failed = 0

    for abs_id, abs_title, audio_source in rows:
        sname, sseq = None, None

        if audio_source == "ABS" and abs_id:
            try:
                item_details = abs_client.get_item_details(abs_id)
                if item_details:
                    meta = item_details.get("media", {}).get("metadata", {})
                    sname, sseq = _extract_series_from_abs_metadata(meta)
            except Exception as e:
                logger.warning(f"Series backfill ABS lookup failed for '{abs_title}': {e}")
                failed += 1
                continue

        if not sname:
            sname, sseq = _extract_series_from_title(abs_title or "")

        if sname:
            updates.append((abs_id, sname, sseq))
            logger.debug(f"Series backfill queued: '{sname}' #{sseq} → '{abs_title}'")

    # Write pass — single transaction, plain SQL
    if updates:
        with db.get_session() as session:
            for abs_id, sname, sseq in updates:
                session.execute(
                    _sa.text("UPDATE books SET series_name = :sname, series_sequence = :sseq WHERE abs_id = :abs_id"),
                    {"sname": sname, "sseq": sseq, "abs_id": abs_id},
                )

    duration = round(_time.time() - start, 1)
    logger.info(
        f"Series backfill complete: updated={len(updates)} skipped={skipped} "
        f"failed={failed} duration={duration}s"
    )
    return jsonify({
        "scanned": len(rows),
        "updated": len(updates),
        "skipped_already_set": skipped,
        "failed": failed,
        "duration_seconds": duration,
        "sample_updates": [{"abs_id": a, "series": s, "seq": q} for a, s, q in updates[:10]],
    }), 200


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
    try:
        token = container.abs_client().token
        base_url = container.abs_client().base_url
        if not token or not base_url:
            return "ABS not configured", 500

        url = f"{base_url.rstrip('/')}/api/items/{abs_id}/cover?token={token}"

        # Stream the response to avoid loading large images into memory
        req = requests.get(url, stream=True, timeout=10)
        if req.status_code == 200:
            from flask import Response
            return Response(req.iter_content(chunk_size=1024), content_type=req.headers.get('content-type', 'image/jpeg'))
        else:
            return "Cover not found", 404
    except Exception as e:
        logger.error(f"❌ Error proxying cover for '{abs_id}': {e}")
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
        logger.error(f"Error fetching Grimmory shelves: {e}")
        return jsonify({"error": str(e)}), 500


def get_abs_libraries():
    """Return available Audiobookshelf libraries."""
    if not container.abs_client().is_configured():
        return jsonify({"error": "Audiobookshelf not configured"}), 400

    libraries = container.abs_client().get_libraries()
    return jsonify(libraries)


def proxy_booklore_audiobook_cover(book_id):
    """Stream a Grimmory audiobook cover through the backend."""
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
        logger.error(f"❌ Error proxying Grimmory audiobook cover for '{book_id}': {e}")
        return "Error loading cover", 500


def proxy_bookorbit_audiobook_cover(book_id):
    """Stream a BookOrbit book cover through the backend."""
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
        logger.error(f"❌ Error proxying BookOrbit cover for '{book_id}': {e}")
        return "Error loading cover", 500


def api_booklore_refresh():
    """Clear Grimmory cache and trigger a full refresh."""
    client = container.booklore_client()
    if not client.is_configured():
        return jsonify({"success": False, "error": "Grimmory not configured"}), 400

    try:
        refreshed = client.clear_and_refresh()
    except Exception as e:
        logger.error(f"❌ Grimmory cache refresh failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    if not refreshed:
        return jsonify({"success": False, "error": "Grimmory refresh failed"}), 500

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
            logger.warning(f"Invalid KOSYNC_PORT '{configured_port}' while testing KOSync settings")

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
    }
    tester = testers.get(service)
    if not tester:
        return jsonify({"ok": False, "message": f"Unknown service: {service}"}), 400
    try:
        return jsonify(tester(payload))
    except Exception as e:
        return jsonify({"ok": False, "message": _test_conn_error(e)})


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


def _test_kosync(enabled: bool, url: str, user: str, key: str) -> dict:
    if not enabled or not url:
        return {"ok": False, "message": "KOSync not configured or disabled"}
    if not user or not key:
        return {"ok": False, "message": "Missing username or password"}

    headers = kosync_auth_headers(user, hash_kosync_key(key))
    healthcheck_status = None
    healthcheck_error = None
    try:
        healthcheck = requests.get(
            _build_test_url(url, "healthcheck"),
            headers=headers,
            timeout=5,
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

    auth = requests.get(_build_test_url(url, "users/auth"), headers=headers, timeout=5)
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


# --- Application Factory ---
def create_app(test_container=None):
    # Under a test container, run deferred work inline so integration tests are
    # deterministic (no background thread races).
    if test_container is not None:
        global _BACKGROUND_TASKS_SYNCHRONOUS
        _BACKGROUND_TASKS_SYNCHRONOUS = True
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
    app.add_url_rule('/api/booklore/libraries', 'get_booklore_libraries', get_booklore_libraries, methods=['GET'])
    app.add_url_rule('/api/booklore/shelves', 'get_booklore_shelves', get_booklore_shelves, methods=['GET'])
    app.add_url_rule('/api/abs/libraries', 'get_abs_libraries', get_abs_libraries, methods=['GET'])
    app.add_url_rule('/api/booklore/refresh', 'api_booklore_refresh', api_booklore_refresh, methods=['POST'])
    app.add_url_rule('/api/bookfusion/device/start', 'api_bookfusion_device_start', api_bookfusion_device_start, methods=['POST'])
    app.add_url_rule('/api/bookfusion/device/poll', 'api_bookfusion_device_poll', api_bookfusion_device_poll, methods=['POST'])
    app.add_url_rule('/api/bookfusion/search', 'api_bookfusion_search', api_bookfusion_search, methods=['GET'])
    app.add_url_rule('/api/bookfusion/link/<abs_id>', 'api_bookfusion_link', api_bookfusion_link, methods=['POST'])
    app.add_url_rule('/api/bookfusion/link/<abs_id>', 'api_bookfusion_unlink', api_bookfusion_unlink, methods=['DELETE'])
    app.add_url_rule('/api/test-connection/<service>', 'test_connection', test_connection, methods=['POST'])

    # Storyteller API routes
    app.add_url_rule('/api/storyteller/search', 'api_storyteller_search', api_storyteller_search, methods=['GET'])
    app.add_url_rule('/api/storyteller/link/<abs_id>', 'api_storyteller_link', api_storyteller_link, methods=['POST'])
    app.add_url_rule('/api/storyteller/backfill', 'api_storyteller_backfill', api_storyteller_backfill, methods=['POST'])
    app.add_url_rule('/api/admin/backfill-series', 'api_series_backfill', api_series_backfill, methods=['POST'])
    app.add_url_rule('/api/admin/debug-abs-series', 'api_debug_abs_series', api_debug_abs_series, methods=['GET'])

    # Forge routes
    app.add_url_rule('/api/forge/search_audio', 'forge_search_audio', forge_search_audio, methods=['GET'])
    app.add_url_rule('/api/forge/search_text', 'forge_search_text', forge_search_text, methods=['GET'])
    app.add_url_rule('/api/forge/process', 'forge_process', forge_process, methods=['POST'])
    app.add_url_rule('/api/alignments/llm-status', 'alignments_llm_status', alignments_llm_status, methods=['GET'])
    app.add_url_rule('/api/alignments/realign', 'alignments_realign', alignments_realign, methods=['POST'])

    @app.route('/api/forge/active', methods=['GET'])
    def forge_active_tasks():
        tasks = set()
        try:
            tasks.update(container.forge_service().active_tasks or set())
        except Exception:
            pass
        try:
            forging_books = database_service.get_books_by_status('forging')
            if isinstance(forging_books, (list, tuple, set)):
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
        logger.warning("Annotation sync daemon failed to start: %s", exc)

    # Re-attach Forge & Match completion watchers orphaned by a restart. The
    # banner/card survive in the DB (status='forging'), but the polling thread
    # that finalizes the forge does not, so resume it here. The registry lets
    # each book resume on its owner's clients (multi-user), not just the admin's.
    try:
        container.forge_service().resume_pending_forge_matches(
            user_client_registry=container.user_client_registry()
        )
    except Exception as exc:
        logger.warning("Forge & Match: resume on startup failed: %s", exc)

    # One-time backfill of StoryGraph ratings for already-linked books.
    # Self-limiting: rows with storygraph_rating_updated_at set are skipped on future startups.
    try:
        from src.services.storygraph_rating_backfill import start_backfill_thread as _start_sg_backfill
        _start_sg_backfill(
            database_service=database_service,
            storygraph_client=container.storygraph_client(),
        )
    except Exception as exc:
        logger.warning("StoryGraph rating backfill thread failed to start: %s", exc)

    # Check ebook source configuration
    booklore_configured = container.booklore_client().is_configured()
    bookorbit_configured = container.bookorbit_client().is_configured()
    books_volume_exists = container.books_dir().exists()

    if booklore_configured:
        logger.info(f"✅ Grimmory integration enabled - ebooks sourced from API")
    elif bookorbit_configured:
        logger.info(f"✅ BookOrbit integration enabled - ebooks sourced from API")
    elif books_volume_exists:
        logger.info(f"✅ Ebooks directory mounted at {container.books_dir()}")
    else:
        logger.info(
            "⚠️  NO EBOOK SOURCE CONFIGURED: No ebook source available. "
            "New book matches will fail. Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD), "
            "enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, BOOKORBIT_PASSWORD), "
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
