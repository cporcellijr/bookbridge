import glob
import gzip
import hashlib
import hmac
import html
import logging
import json
import contextvars
import functools
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
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
import schedule
from dependency_injector import providers
from flask import Flask, Response, render_template, render_template_string, request, redirect, url_for, jsonify, session, send_from_directory, make_response, g, current_app, flash, has_request_context, abort
from jinja2 import TemplateNotFound
from functools import wraps
from src.utils.local_file_scope import acting_user_is_regular, suggestions_exclude_local_files
from src.utils.user_context import (
    set_current_user_id, reset_current_user_id,
    set_current_user_credentials, reset_current_user_credentials,
    get_current_user_credentials, get_current_user_id,
)
from src.utils.display_settings import normalize_display_value, resolve_display_settings
from src.utils.user_config import PER_USER_PREFERENCE_KEYS, resolve_setting, user_preference_enabled, user_setting
from src.utils.user_config import global_fallback_allowed as _global_fallback_allowed
from src.utils.user_config import SERVICE_ENABLE_KEYS
from src.utils.kosync_url import is_builtin_kosync_url

from src.utils.config_loader import (
    ConfigLoader, DEFAULT_CONFIG, KNOWN_SETTING_KEYS, MERGED_BOOL_SETTINGS, MERGED_SETTINGS, merged_mixed_settings,
    RESTART_REQUIRED_SETTINGS, env_truthy, merged_setting_values,
)
from src.utils.cache_paths import safe_cache_path, safe_library_path, is_plain_basename, contained_library_file, library_roots
from src.utils.ebook_utils import (
    LINKABLE_EBOOK_EXTENSIONS,
    LRUCache,
    is_comic_ebook_filename,
    is_in_hidden_folder,
    is_linkable_ebook_filename,
)
from src.utils.ebook_sources import is_grimmory_source, local_ebook_filename, normalize_ebook_source
from src.utils.local_ebook_metadata import read_local_ebook_metadata, read_local_ebook_series
from src.utils.logging_utils import memory_log_handler, LOG_PATH
from src.utils.logging_utils import sanitize_log_data
from src.utils.logging_utils import get_persistent_condition_logger
from src.services.diagnostics import setup_diagnostics_logging
from src.services.aligned_shelf import defer_shelf_add, shelf_add_waits_for_alignment
from src.services.job_queue_view import build_job_queue_view
from src.services.transcription_preflight import pair_key as transcription_preflight_pair_key, add_library_comparison
from src.services import whats_new
from src.api.api_clients import ABS_DISABLED_SENTINEL, is_abs_disabled_value
from src.api.kosync_server import kosync_sync_bp, kosync_admin_bp, init_kosync_server, signal_manifest_rebuild
# This import adds the position-preview routes to kosync_admin_bp before
# setup_dependencies registers the blueprint.
from src.api import reading_position_preview_routes  # noqa: E402,F401
from src.api.admin_api import admin_api_bp, init_admin_api
from src.api.alignment_worker_api import alignment_worker_api_bp, init_alignment_worker_api
from src.services.alignment_workers import AlignmentWorkerCoordinator
from src.api.hardcover_routes import hardcover_bp, init_hardcover_routes
from src.api.storygraph_routes import storygraph_bp, init_storygraph_routes
from src.api.goodreads_routes import goodreads_bp, init_goodreads_routes
from src.api.bookfusion_upload_client import extract_epub_metadata, _S3_TIMEOUT_LARGE
from src.version import APP_VERSION, get_update_status
from src.db.models import State, JOB_KIND_ALIGNMENT, JOB_KIND_READALONG
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.services.audio_source_adapters import AudioResult, ABSAudioSourceAdapter, BookLoreAudioSourceAdapter, BookOrbitAudioSourceAdapter
from src.services.audio_cache_ownership import inventory_audio_cache, remove_book_cache_dirs, remove_owned
from src.services.current_audio_chapter import resolve_current_chapter
from src.services.cover_thumbnail_service import cover_thumbnail_cache
from src.utils.storyteller_transcript import StorytellerTranscript
from src.services.readalong_delivery import (
    deliver_readalong_epub,
    resolve_audiobook_folder,
    remove_readalong_epub as _remove_readalong_epub_file,
    _readalong_filename,
    is_generated_readalong_epub,
)
from src.utils.kosync_headers import kosync_request_kwargs
from src.utils.series_metadata import (
    extract_series_from_abs_metadata as _series_from_abs_metadata,
    extract_series_from_library_detail as _series_from_library_detail,
    extract_series_from_title as _series_from_title,
    normalize_series_key,
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
abs_socket_manager = None
SUGGESTIONS_SCAN_JOBS = {}
SUGGESTIONS_SCAN_JOBS_LOCK = threading.Lock()
# One Suggestions scan runs at a time across all readers (CPU, the Ollama judge and
# provider logins are shared); the others wait as 'queued'.
SUGGESTIONS_SCAN_SLOT = threading.Lock()
_SCAN_JOB_ACTIVE = ("queued", "running")
SUGGESTIONS_SCAN_JOB_TTL_SECONDS = 3600
SUGGESTIONS_STATE_STORE = {}
SUGGESTIONS_STATE_LOCK = threading.Lock()
SUGGESTIONS_STATE_TTL_SECONDS = 86400
# Per-user credential key (not a user-editable setting) recording the last
# APP_VERSION the "what's new" banner/page was shown/dismissed for.
WHATS_NEW_SEEN_KEY = "WHATS_NEW_SEEN_VERSION"
SUGGESTIONS_CACHE_FILE_NAME = "suggestions_scan_cache.json"
SUGGESTIONS_CACHE_LOCK = threading.RLock()
# Batch-match queue lives on disk (under DATA_DIR) instead of the Flask session cookie:
# the cookie caps at ~4KB (~a dozen items) and is per-browser; the file store is
# unbounded, survives container rebuilds, and is shared across the admin's tabs.
MATCH_QUEUE_FILE_NAME = "match_queue.json"
MATCH_QUEUE_LOCK = threading.RLock()
# Read-only visibility of Add Book batches already handed to background threads.
# This does not control, persist, or reorder the workers.
_VISIBLE_BATCH_LOCK = threading.Lock()
_VISIBLE_BATCHES = {}
_VISIBLE_BATCH_LABELS = {
    'batch-forge-match': 'Forge & Match batch',
    'batch-match-process': 'Match batch',
    'add-book-forge-only': 'Forge Only batch',
}
_READALONG_GENERATION_LOCK = threading.Lock()
_ACTIVE_READALONG_GENERATIONS = set()


def _claim_readalong_generation(abs_id: str) -> bool:
    """Reserve one in-process read-along generation for ``abs_id``."""
    with _READALONG_GENERATION_LOCK:
        if abs_id in _ACTIVE_READALONG_GENERATIONS:
            return False
        _ACTIVE_READALONG_GENERATIONS.add(abs_id)
        return True


def _release_readalong_generation(abs_id: str) -> None:
    """Release the in-process read-along generation reservation."""
    with _READALONG_GENERATION_LOCK:
        _ACTIVE_READALONG_GENERATIONS.discard(abs_id)
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

    # Forge workers run in their own module and thread. Inject the live
    # read-along worker/admission hooks so the production ``__main__`` entry
    # point never imports a second, uninitialized ``src.web_server`` module.
    forge = container.forge_service()
    forge.readalong_epub_worker = _readalong_epub_worker
    forge.readalong_generation_admitter = _claim_readalong_generation
    forge.readalong_generation_releaser = _release_readalong_generation
    # The background alignment job (Match All's Storyteller-free path) hands a
    # finished book to the same read-along dispatcher the Forge path uses.
    manager.readalong_intent_dispatcher = forge.generate_readalong_if_requested

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
    init_goodreads_routes(database_service, container)
    app.register_blueprint(goodreads_bp)

    # Token-authenticated admin API (disabled unless ADMIN_API_TOKEN is set)
    init_admin_api(
        database_service, _share_library_with_new_user, _after_user_integrations_saved,
        _after_user_account_changed, on_user_enabled=_share_library_with_returning_user,
    )
    app.register_blueprint(admin_api_bp)

    # Token-authenticated remote alignment workers (disabled unless ALIGNMENT_WORKER_TOKEN is set)
    init_alignment_worker_api(AlignmentWorkerCoordinator(manager, database_service))
    app.register_blueprint(alignment_worker_api_bp)

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
from src.web import user_clients as _user_clients
_user_clients._WEB_MODULE = sys.modules[__name__]
from src.web.user_clients import (
    get_audiobooks_conditionally,
    _normalize_abs_form_value,
    _display_abs_server,
    _UnavailableClient,
    _GlobalClients,
    uc,
    _scan_user_clients,
    _acting_user_may_use_global_config,
    _client_bundle_kwargs,
    _optional_client_configured,
)





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
_LOGIN_FAILURE_MAX_KEYS = 10_000
_LOGIN_USERNAME_KEY_CHARS = 256

# Global engine configuration, library-wide maintenance and logs are admin-only.
# Reader pages, including Stats, resolve the acting user's data and credentials.
_ADMIN_ONLY_ENDPOINTS = {
    'settings',
    'logs_view', 'api_logs', 'api_logs_live', 'view_log',
    'clean_cache', 'api_series_backfill', 'api_debug_abs_series', 'api_storyteller_backfill',
    'admin_users', 'admin_user_integrations',
    'api_restart', 'test_connection',
    'get_booklore_libraries', 'get_booklore_shelves', 'get_watch_shelves', 'get_abs_libraries',
    'api_booklore_refresh', 'alignments_llm_status', 'alignments_realign', 'alignments_restore',
    'kosync_admin.api_get_kosync_documents',
    'kosync_admin.api_link_kosync_document',
    'kosync_admin.api_unlink_kosync_document',
    'kosync_admin.api_delete_kosync_document',
    'my_reports',
    'api_diagnostics_submissions',
    # Legacy Forge page APIs: no UI calls them since Search replaced the page, and they
    # search and stage the server's local ebook folder.
    'forge_search_audio', 'forge_search_text', 'forge_process',
}


from src.web import session_auth as _session_auth
_session_auth._WEB_MODULE = sys.modules[__name__]
from src.web.session_auth import (
    current_user,
    admin_required,
    _safe_next_url,
    _trusted_proxy_networks,
    _remote_auth_peer_trusted,
    _client_address,
    _remote_auth_user,
    _establish_session_and_redirect,
    session_marker,
)


# ---------------- i18n / UI LANGUAGE ----------------
# The interface language. Resolution order: the user's saved `users.language`
# (when it names a language we actually have a catalog for), then the request's
# Accept-Language, then English. Wrapped strings and the catalogs are gettext
# (Flask-Babel); only user-facing display text is translated — log messages,
# setting keys and API/JSON field names stay English by design.

# Babel wants `_`-style codes ("ru_RU"); browsers send tags ("ru-RU"). Everything
# is reduced to the primary subtag, which is all we key catalogs and the column on.
_BASE_LOCALE = "en"


from src.web import request_context as _request_context
_request_context._WEB_MODULE = sys.modules[__name__]
from src.web.request_context import (
    _normalize_locale,
    locale_display_name,
    get_locale,
    _request_wants_json,
    inject_csrf_script,
    gzip_html_response,
    _bind_request_user_context,
    _release_request_user_context,
    ui_template,
    inject_global_vars,
)


def translations_dir() -> str:
    """Directory holding the per-language catalogs (translations/<lang>/...).

    Resolved from the app root the same way STATIC_DIR/TEMPLATE_DIR are, with an
    env override so a deployment can point elsewhere. This module runs as
    /app/src/web_server.py in the image, so the root is /app.
    """
    override = os.environ.get("TRANSLATIONS_DIR")
    if override:
        return override
    app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(app_root, "translations")


def available_locales() -> tuple:
    """UI languages that actually have a catalog, plus the English base.

    Derived from the catalogs on disk, not a hardcoded list: a language the user
    can pick must be a language we can render. Until a `ru` catalog ships, `ru`
    is simply not offered — the picker never shows a choice that does nothing.

    Cached: the catalogs only change on deploy, and this is called several times
    per request. Call available_locales.cache_clear() after adding one.
    """
    locales = [_BASE_LOCALE]
    try:
        from babel import Locale

        directory = translations_dir()
        if os.path.isdir(directory):
            for name in sorted(os.listdir(directory)):
                candidate = _normalize_locale(name)
                if not candidate or candidate in locales:
                    continue
                if os.path.isfile(os.path.join(directory, name, "LC_MESSAGES", "messages.mo")):
                    try:
                        Locale.parse(candidate)
                    except Exception:
                        continue
                    locales.append(candidate)
    except Exception:
        # Never let catalog discovery break a page render.
        return (_BASE_LOCALE,)
    return tuple(locales)


available_locales = functools.lru_cache(maxsize=1)(available_locales)




_UNAVAILABLE_CLIENT = _UnavailableClient()




_global_clients = _GlobalClients()


# Lets background work (e.g. batch-match processing) re-bind the request's client
# bundle onto its own thread, so uc()-internal helpers resolve the right user's
# clients instead of silently falling back to the global bundle (current_user()
# reads the Flask request context, which a worker thread doesn't have).
_active_bundle: "contextvars.ContextVar" = contextvars.ContextVar("active_client_bundle", default=None)

# When True, _spawn_user_background runs inline instead of on a daemon thread. Set by
# create_app() under a test container so integration tests stay deterministic.
_BACKGROUND_TASKS_SYNCHRONOUS = False












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


from src.web import background_tasks as _background_tasks
_background_tasks._WEB_MODULE = sys.modules[__name__]
from src.web.background_tasks import (
    _tracker_automatch_worker,
    _visible_batch_items,
    _spawn_user_background,
)


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
    if request.blueprint in ('admin_api', 'alignment_worker_api'):  # bearer-token APIs, own auth
        return None
    user = current_user()
    if user is None:
        remote_user = _remote_auth_user()
        if remote_user is not None:
            session['user_id'] = remote_user.id
            session['username'] = remote_user.username
            session['role'] = remote_user.role
            session['session_marker'] = session_marker(remote_user)
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


_LOOPBACK_PROXY_NETWORKS = ('127.0.0.0/8', '::1/128')


from src.web.routes.login import login, setup, logout


from src.web.routes.account import account, account_language, account_interface, account_display, account_display_page, account_integrations, account_test_connection, account_abs_libraries, account_booklore_libraries, account_bookorbit_scan_libraries










from src.web import account_helpers as _account_helpers
_account_helpers._WEB_MODULE = sys.modules[__name__]
from src.web.account_helpers import (
    _TEST_CONNECTION_FIELDS,
    _display_settings_for,
    _apply_user_integrations,
    _after_user_integrations_saved,
    _posted_user_test_credentials,
    _reader_scan_libraries,
    _invalidate_user_clients,
    _refresh_user_abs_listener,
    _after_user_account_changed,
    _share_library_with_new_user,
    _share_library_with_returning_user,
)




# Library-lookup credentials the primary admin's account also lends to the
# engine's global singletons (shelf-watch, scans, suggestions, ABS socket,
# manifest). Mirrored to the global settings when the primary admin saves.
from src.utils.user_config import ENGINE_MIRROR_KEYS as _ENGINE_MIRROR_KEYS


from src.web.routes.admin_users import admin_users, admin_user_integrations, admin_user_test_connection, admin_user_abs_libraries, admin_user_booklore_libraries, admin_user_bookorbit_scan_libraries, admin_user_bookfusion_device_start, admin_user_bookfusion_device_poll


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
    "Goodreads": "goodreads",
    "Shelfmark": "shelfmark",
}


















# User-management actions accepted by both the legacy /admin/users page and the
# Settings → Users tab (which posts to /settings).
# Every action _apply_user_admin_action handles MUST be listed here: POST /settings
# routes anything missing into the settings-save branch instead, which writes the
# whole settings form and restarts.
_USER_ADMIN_ACTIONS = {'create', 'reset_password', 'toggle_active', 'set_role', 'delete', 'share_library'}



from src.web import admin_actions as _admin_actions
_admin_actions._WEB_MODULE = sys.modules[__name__]
from src.web.admin_actions import (
    _apply_user_admin_action,
)




# ---------------- BOOK LINKER HELPERS ----------------
from src.services.alignment_service import ingest_storyteller_transcripts









from src.web import diagnostics_helpers as _diagnostics_helpers
_diagnostics_helpers._WEB_MODULE = sys.modules[__name__]
from src.web.diagnostics_helpers import (
    _diagnostics_receiver_context,
    _run_diagnostics_send,
)


from src.web import kosync_login_mirror as _kosync_login_mirror
_kosync_login_mirror._WEB_MODULE = sys.modules[__name__]
from src.web.kosync_login_mirror import (
    KOSYNC_MIRRORED_LOGIN_KEY,
    _kosync_login_fingerprint,
    _mirror_kosync_login,
    _mirror_kosync_logins_from_grimmory,
)








from src.web import sync_daemon as _sync_daemon
_sync_daemon._WEB_MODULE = sys.modules[__name__]
from src.web.sync_daemon import (
    sync_daemon,
    _is_abs_audio_book,
    _reconcile_abs_collection,
)







# ---------------- ORIGINAL ABS-KOSYNC HELPERS ----------------

from src.web import kosync_ebook_identity as _kosync_ebook_identity
_kosync_ebook_identity._WEB_MODULE = sys.modules[__name__]
from src.web.kosync_ebook_identity import (
    find_ebook_file,
    get_kosync_id_for_ebook,
    _compute_storyteller_trilink_kosync_id,
)


from src.web import matched_ebook_delivery as _matched_ebook_delivery
_matched_ebook_delivery._WEB_MODULE = sys.modules[__name__]
from src.web.matched_ebook_delivery import (
    _is_storyteller_artifact_filename,
    _is_abs_hosted_ebook_filename,
    _aligned_shelf_target,
    _aligned_shelf_assignment,
    _reconcile_aligned_shelf,
    _shelve_matched_ebook,
    _shelve_saved_ebook,
    _publish_saved_ebook_to_readest,
    _download_storyteller_artifact,
    _resolve_abs_chapters_for_storyteller_ingest,
)


from src.web import mapping_status as _mapping_status
_mapping_status._WEB_MODULE = sys.modules[__name__]
from src.web.mapping_status import (
    _mapping_identity_changes,
    _preserve_or_reset_mapping_status,
    _adopt_kosync_progress_for_book,
    _storyteller_register_file_hashes,
)


from src.web import storyteller_library_watch as _storyteller_library_watch
_storyteller_library_watch._WEB_MODULE = sys.modules[__name__]
from src.web.storyteller_library_watch import (
    _storyteller_auto_link_candidates,
    _storyteller_user_library_pass,
    _run_storyteller_library_hourly,
    _upsert_storyteller_mapping,
)


_STORYTELLER_PASS_LOCK = threading.Lock()
_STORYTELLER_PASSES_IN_PROGRESS = set()


EBOOK_ALREADY_MAPPED = "This ebook is already in BookBridge"


from src.web import suggestions_audio_catalog as _suggestions_audio_catalog
_suggestions_audio_catalog._WEB_MODULE = sys.modules[__name__]
from src.web.suggestions_audio_catalog import (
    _ebook_edition_label,
    _record_suggestions_source_state,
    get_searchable_audiobooks,
    _punctuation_search_key,
    _punctuation_search_fragment,
    _audiobook_search_variants,
    _search_audiobooks_with_fallback,
    get_suggestion_audiobooks,
    EbookResult,
)


from src.web import suggestions_ebook_catalog as _suggestions_ebook_catalog
_suggestions_ebook_catalog._WEB_MODULE = sys.modules[__name__]
from src.web.suggestions_ebook_catalog import (
    _ebook_is_provider,
    _search_ebooks_with_fallback,
    _first_not_none,
    _ebook_title_key,
    _scan_excludes_local_files,
    _submitted_ebook_path,
    _refuse_regular_user_local_selection,
    _build_local_ebook_title_index,
    get_searchable_ebooks,
    _promote_authoritative_ebook_matches,
)


# Non-ABS audiobook providers. Their mappings live under a prefixed bridge key
# ('booklore:<id>' / 'bookorbit:<id>') instead of an ABS item id.
_LIBRARY_AUDIO_SOURCES = ("BookLore", "BookOrbit")
_AUDIO_BRIDGE_PREFIXES = {"booklore": "booklore", "bookorbit": "bookorbit"}


from src.web import audio_source_adapters as _audio_source_adapters
_audio_source_adapters._WEB_MODULE = sys.modules[__name__]
from src.web.audio_source_adapters import (
    _AUDIO_SOURCE_BADGE_LABELS,
    _audio_source_display_name,
    _audio_source_from_bridge_key,
    suggestion_source_badge,
    _build_bridge_key,
    _normalize_text_source_type,
    _safe_local_source_path,
    _build_forge_text_item,
    _parse_audio_duration,
)


from src.web import mapping_creation as _mapping_creation
_mapping_creation._WEB_MODULE = sys.modules[__name__]
from src.web.mapping_creation import (
    _set_bookorbit_link_for_current_user,
    _create_or_update_library_audio_mapping,
    _create_or_update_audio_only_mapping,
    _ensure_bookfusion_ebook_cached,
    _link_bookfusion_ebook_source,
    _create_or_update_bookfusion_progress_mapping,
)



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

from src.web.routes.system import api_health, api_restart, clean_inactive_cache, test_connection


from src.web.routes.settings import settings

from src.web import dashboard_metadata as _dashboard_metadata
_dashboard_metadata._WEB_MODULE = sys.modules[__name__]
from src.web.dashboard_metadata import (
    SERIES_PREVIEW_LIMIT,
    get_abs_author,
    _coerce_author_display,
    _extract_series_from_abs_metadata,
    _extract_series_from_booklore_metadata,
    _normalize_series_key,
    _format_series_sequence,
    _series_preview_books,
    _finalize_series_group,
    _group_dashboard_mappings_by_series,
    _dashboard_filename_key,
    _index_cached_booklore_books,
    _viewer_booklore_index,
    _get_cached_booklore_book,
    _get_cached_ebook_display_metadata,
    _coerce_dashboard_rating,
    _coerce_dashboard_count,
    _get_cached_goodreads_rating,
    _get_cached_booklore_id,
    _get_dashboard_display_filename,
    _normalize_dashboard_display_value,
    _parse_dashboard_filename_fallback,
    _looks_like_dashboard_filename_title,
    _should_override_dashboard_base_title,
    _get_storyteller_display_metadata,
    _get_cached_storyteller_display_metadata,
    _resolve_dashboard_display_metadata,
)


from src.web import dashboard_sync_warning as _dashboard_sync_warning
_dashboard_sync_warning._WEB_MODULE = sys.modules[__name__]
from src.web.dashboard_sync_warning import (
    _AUDIO_AXIS_SYNC_CLIENTS,
    _DASHBOARD_SYNC_WARNING_CACHE,
    _compute_dashboard_sync_warning_pct,
    _dashboard_sync_warning_cache_key,
    _dashboard_sync_warning_candidates,
    _dashboard_text_axis_pct,
    _get_dashboard_sync_warning_clients,
    _storyteller_transcript_source,
)




# Clients that report progress on the audio-time axis (elapsed seconds /
# duration) rather than the ebook-text axis (characters / total). Their raw
# percentage is not directly comparable to ebook clients, so it is mapped onto
# the text axis via the alignment map before the drift comparison.




# Drift is computed for every visible book on every dashboard render, and an
# audio client's conversion loads that book's alignment map — a 10-15MB JSON blob
# against a 3-entry cache. The result only moves when a position moves, so it is
# memoized on the exact inputs that produced it (issue #412).








from src.web import shelf_watch_service as _shelf_watch_service
_shelf_watch_service._WEB_MODULE = sys.modules[__name__]
from src.web.shelf_watch_service import (
    _shelf_watch_clients_for,
    _queue_item_shelf_watch_metadata,
    _watch_library_key,
    _is_other_book_in_watched_library,
    _watch_suggestion_review,
    _complete_shelf_watch_approval,
)


from src.web import dashboard_projection as _dashboard_projection
_dashboard_projection._WEB_MODULE = sys.modules[__name__]
from src.web.dashboard_projection import (
    _format_dashboard_last_sync,
    _build_dashboard_integrations,
    _group_dashboard_states_by_book,
    _dashboard_leader_service,
    _format_strip_slot,
    _build_format_strip,
    _browser_cover_url,
    _is_synthetic_bridge_key,
    _sanitize_cover_urls,
    _public_link_base,
    _prefetch_bookfusion_links,
    _bookorbit_source_ids,
    _prefetch_bookorbit_authors,
    _bookorbit_author_for_book,
    _build_dashboard_mapping,
    _build_dashboard_mappings,
)


_FORMAT_STRIP_EBOOK_CLIENTS = ("bookorbit", "booklore", "kavita", "cwa", "bookfusion", "absebook")
_FORMAT_STRIP_SERVICE_NAMES = {
    "abs": "Audiobookshelf", "absebook": "Audiobookshelf", "bookorbit": "BookOrbit",
    "bookorbitaudio": "BookOrbit", "booklore": "Grimmory", "bookloreaudio": "Grimmory",
    "kavita": "Kavita", "cwa": "Calibre-Web", "bookfusion": "BookFusion",
    "storyteller": "Storyteller", "kosync": "KOReader",
    "BookOrbit": "BookOrbit", "BookLore": "Grimmory", "Booklore": "Grimmory", "Kavita": "Kavita", "CWA": "Calibre-Web",
    "ABS": "Audiobookshelf", "BookFusion": "BookFusion",
}


from src.web import library_access as _library_access
_library_access._WEB_MODULE = sys.modules[__name__]
from src.web.library_access import (
    _dashboard_visible_books_for_user,
    _claim_book_for_current_user,
    _claim_book_for_user_id,
    _active_bookfusion_link_user_id,
    _persist_bookfusion_link_for_user_id,
    _persist_bookfusion_link_for_current_user,
    audiobook_matches_search,
)


def _build_whats_new_context(user) -> Optional[dict]:
    """Compute the "what's new after upgrade" banner context for the Library
    page, or None to show nothing. Never raises into the page: any failure
    is logged at debug and treated as "no banner".

    When no seen-version has been stored yet for the user and this looks
    like a brand-new account (created by this same process), the current
    version is silently stored as a baseline instead of announcing it.
    """
    if user is None:
        return None
    try:
        stored = database_service.get_user_credentials(user.id).get(WHATS_NEW_SEEN_KEY)
        decision = whats_new.should_show_banner(
            app_version=APP_VERSION,
            stored_value=stored,
            user_created_at=getattr(user, "created_at", None),
            process_started_at=whats_new.PROCESS_STARTED_AT,
        )
        if decision == "store_current":
            database_service.set_user_credential(user.id, WHATS_NEW_SEEN_KEY, APP_VERSION)
            return None
        if decision != "show":
            return None
        return {
            "version": APP_VERSION,
            "action_items": whats_new.get_action_required_items(),
        }
    except Exception as e:
        logger.debug(f"Could not compute whats-new banner state: {e}", exc_info=True)
        return None


from src.web.routes.whats_new import whats_new_page, whats_new_dismiss




from src.web import dashboard_context as _dashboard_context
_dashboard_context._WEB_MODULE = sys.modules[__name__]
from src.web.dashboard_context import (
    _dashboard_mappings_for_user,
    _warm_dashboard_sync_warnings,
    _pick_hero_mapping,
    _hero_leader_service,
    _hero_leader_percent,
    _hero_plain_text,
    _hero_synopsis_source,
    _hero_cached_synopsis,
    _hero_daily_totals,
    _hero_listening_piece,
    _hero_reading_piece,
    _hero_ebook_fraction,
    _hero_streak_days,
)


# Striped per-book locks prevent duplicate concurrent synopsis source reads.
_HERO_SYNOPSIS_LOCKS = tuple(threading.Lock() for _ in range(32))
# Sources answer None on a transient failure too, so an empty result is retried.
_HERO_SYNOPSIS_RECHECK_SECONDS = 7 * 86400


from src.web.routes.book import book_page, api_library_hero, api_book_synopsis




from src.web.routes.library import index


from src.web.routes.book_actions import api_current_audio_chapter, delete_mapping, delete_selected_mappings, clear_progress, remap_alignment, sync_now, mark_complete, update_hash




from src.web.routes.processing import job_queue_page, job_queue_reorder, job_queue_preflight_ack, shelfmark








from src.web.routes.forge import forge, forge_search_audio, forge_search_text, forge_process











from src.web.routes.alignments import alignments_llm_status, alignments_realign, alignments_restore






from src.web.routes.matching import match, batch_match, add_book, _legacy_book_linker_redirect

from src.web import match_queue_items as _match_queue_items
_match_queue_items._WEB_MODULE = sys.modules[__name__]
from src.web.match_queue_items import (
    _create_ebook_only_mapping_from_queue_item,
    _create_audio_only_mapping_from_queue_item,
    _record_readalong_intent_for_match,
    _record_forge_match_job,
    _queue_item_from_match_form,
    _queue_item_from_suggestion,
)


from src.web import match_queue_processor as _match_queue_processor
_match_queue_processor._WEB_MODULE = sys.modules[__name__]
from src.web.match_queue_processor import (
    _process_forge_only_queue,
    _process_batch_queue,
    _process_forge_match_queue,
)








from src.web import suggestions_discovery as _suggestions_discovery
_suggestions_discovery._WEB_MODULE = sys.modules[__name__]
from src.web.suggestions_discovery import (
    _suggestions_search_results,
    _legacy_search_redirect,
    _suggestions_ebook_result,
    _resolve_suggestions_redesign_ebook_create,
    _get_suggestions_service,
    _get_ignored_suggestion_source_ids,
    _suggestions_discovery_sources_complete,
    _invalidate_suggestions_pair_cache,
    _reconcile_discovery_with_pair_matches,
)






from src.web import suggestions_scan_workflow as _suggestions_scan_workflow
_suggestions_scan_workflow._WEB_MODULE = sys.modules[__name__]
from src.web.suggestions_scan_workflow import (
    scan_library_suggestions,
    _prune_suggestions_scan_jobs,
    _auto_match_threshold,
    _is_same_folder_match,
    _auto_match_suggestions,
    _run_suggestions_scan_job,
    _get_suggestions_scan_job,
    _running_suggestions_scan_job_id,
    _owned_suggestions_scan_job,
)


from src.web import suggestions_scan_scheduler as _suggestions_scan_scheduler
_suggestions_scan_scheduler._WEB_MODULE = sys.modules[__name__]
from src.web.suggestions_scan_scheduler import (
    SUGGESTIONS_AUTO_SCAN_STATE_KEY,
    SUGGESTIONS_AUTO_SCAN_MIN_MINUTES,
    _SUGGESTIONS_AUTO_SCAN_JOB,
    _CRON_FIELDS,
    _SUGGESTIONS_WEEKDAYS,
    parse_cron_expression,
    cron_matches,
    _suggestions_full_refresh_schedule,
    _suggestions_full_refresh_due,
    _suggestions_full_refresh_done,
    _suggestions_auto_scan_due,
    _run_scheduled_suggestions_scan,
    _suggestions_auto_scan_tick,
    _start_suggestions_scan_job,
)























from src.web import suggestions_state_store as _suggestions_state_store
_suggestions_state_store._WEB_MODULE = sys.modules[__name__]
from src.web.suggestions_state_store import (
    _clear_legacy_suggestions_session_payload,
    _prune_suggestions_state_store,
    _default_suggestions_state,
    _get_suggestions_state,
    _suggestions_cache_scope_key,
    _suggestions_cache_file_path,
    _empty_suggestions_cache_payload,
    _load_persisted_suggestions_cache,
    _save_persisted_suggestions_cache,
    _rehydrate_suggestions_state_from_cache,
    _persist_suggestions_state,
)


from src.web import match_queue_store as _match_queue_store
_match_queue_store._WEB_MODULE = sys.modules[__name__]
from src.web.match_queue_store import (
    _match_queue_file_path,
    _read_match_queue_unlocked,
    _write_match_queue_unlocked,
    _normalize_match_queue_user_id,
    _match_queue_scope,
    _match_queue_item_visible,
    _match_queue_stamp,
    _clear_match_queue_for_user_id,
    _load_match_queue,
    _save_match_queue,
    _match_queue_add,
    _match_queue_move,
    _match_queue_remove,
    _match_queue_clear,
    _match_queue_drain,
    _match_queue_response,
    _apply_queue_readalong_request,
    _suggestion_bulk_block,
)


from src.web.routes.suggestions import suggestions_page, suggestions_scan_status, get_suggestions, dismiss_suggestion, ignore_suggestion, clear_stale_suggestions




from src.web import mapping_cleanup as _mapping_cleanup
_mapping_cleanup._WEB_MODULE = sys.modules[__name__]
from src.web.mapping_cleanup import (
    cleanup_mapping_resources,
)


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


_FOREIGN_MAPPING_MESSAGE = (
    "Forbidden: another reader's mapping already uses this book. Ask an admin to change it."
)


from src.web import book_access as _book_access
_book_access._WEB_MODULE = sys.modules[__name__]
from src.web.book_access import (
    _acting_user_may_modify_book,
    _existing_mapping_access,
    _may_absorb_mapping,
    _may_replace_mapping,
    _hash_owned_by_foreign_mapping,
    _forbidden_book_response,
    _current_user_claimed_abs_ids,
    _kosync_document_visible_to_user,
    _book_claimed_by_current_scope,
    _serialize_kosync_document_for_ui,
    _get_visible_kosync_document_or_response,
    _delete_or_unlink_book,
)


from src.web.routes.me import api_me_kosync_documents, api_me_link_kosync_document, api_me_unlink_kosync_document, api_me_delete_kosync_document, api_me_books


















# Human-readable labels for the `Job.stage` values `readalong_builder.py` /
# `readalong_delivery.py` report via the progress callback below. Falls back
# to the raw stage string (or "Working") for anything not in this map --
# never an error, since a future stage this map hasn't been updated for
# should still show *something* rather than break the status poll.
from src.web import readalong_generation_jobs as _readalong_generation_jobs
_readalong_generation_jobs._WEB_MODULE = sys.modules[__name__]
from src.web.readalong_generation_jobs import (
    _READALONG_STAGE_LABELS,
    _readalong_stage_label,
    _readalong_epub_worker,
)







from src.web.routes.readalong import generate_readalong_epub, readalong_epub_status, remove_readalong_epub_route










from src.web import cover_freshness as _cover_freshness
_cover_freshness._WEB_MODULE = sys.modules[__name__]
from src.web.cover_freshness import (
    _extracted_cover_is_stale, _schedule_cover_recheck, _recheck_cover,
    _thumb_version, _seed_thumb_versions, thumb_cover_src,
    _COVER_FRESH_TTL_SECONDS, _cover_fresh_until, _cover_recheck_pool,
    _thumb_versions, _thumb_versions_seeded_dir,
)

# Missing covers retry sooner than already-extracted covers.
_COVER_MISSING_RETRY_SECONDS = 300
# Short browser revalidation for unversioned extracted covers.
_COVER_BROWSER_MAX_AGE_SECONDS = 300
# Long browser caching of versioned thumbnails.
_THUMB_IMMUTABLE_CACHE = 'private, max-age=31536000, immutable'



def _send_cover_cached(filename: str):
    """Serve an extracted cover with a browser cache window (see the constant)."""
    response = send_from_directory(
        COVERS_DIR, filename, max_age=_COVER_BROWSER_MAX_AGE_SECONDS
    )
    response.headers['Cache-Control'] = (
        f'private, max-age={_COVER_BROWSER_MAX_AGE_SECONDS}'
    )
    return response


from src.web.routes.covers import serve_cover, serve_thumbnail_cover, proxy_cover, proxy_booklore_audiobook_cover, proxy_booklore_book_cover, proxy_bookorbit_audiobook_cover, proxy_kavita_cover



from src.web.routes.storyteller import api_storyteller_search, api_storyteller_link, api_storyteller_backfill




from src.web import reading_stats as _reading_stats
_reading_stats._WEB_MODULE = sys.modules[__name__]
from src.web.reading_stats import (
    _get_stats_timezone,
    _build_stats_cache_key,
    _read_cached_stats,
    _write_cached_stats,
    _date_series,
    _activity_dates_from_daily,
    _calculate_current_streak_from_dates,
    _normalize_abs_author,
    _recent_daily_from_mapping,
    _heatmap_window,
    _heatmap_from_mapping,
    _summarize_activity,
    _normalize_listening_session,
    _years_from_days,
    _abs_finished_books,
    _build_listening_yearly_recap,
    _build_listening_book_list,
    _abs_last_listened,
    _abs_listening_book_list,
    _abs_book_listening_detail,
    _local_listening_stats,
    _stats_abs_client,
    _build_listening_stats_payload,
    _build_reading_stats_payload,
    _merge_daily_activity,
    _merge_heatmap_activity,
    _merge_recent_sessions,
    _merge_book_lists,
    _build_combined_yearly_recap,
    _build_combined_stats_payload,
    _build_dashboard_progress_rows,
)


# The stats page draws the 53 weeks ending this week. One extra day covers a
# browser whose date is behind the server's.
_HEATMAP_DAYS = 371


from src.web.routes.stats import (
    stats_view,
    api_stats,
    api_stats_reading_day,
    api_stats_reading_calendar,
    api_stats_book_detail,
    api_stats_yearly_recap,
)


from src.web.routes.status import api_status, api_status_progress




from src.web.routes.logs import (
    logs_view,
    api_logs,
    api_logs_live,
    view_log,
)


def _tail_text_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return list(deque(f, maxlen=max_lines))
    except OSError:
        return []


# ---------------- SUGGESTION API ROUTES ----------------










from src.web import storyteller_maintenance as _storyteller_maintenance
_storyteller_maintenance._WEB_MODULE = sys.modules[__name__]
from src.web.storyteller_maintenance import (
    _extract_series_from_title,
    _run_storyteller_backfill,
    _series_backfill_clients,
    _series_refresh_action,
    _series_seq_equal,
)












from src.web.routes.admin_maintenance import api_series_backfill, api_audio_repoint_plan, api_audio_repoint_apply, api_audio_repoint_undo, api_debug_abs_series












# --- Logger setup (already present) ---
logger = logging.getLogger(__name__)

from src.web.routes.integration_lookups import get_booklore_libraries, get_booklore_shelves, get_watch_shelves, get_abs_libraries, api_booklore_refresh

from src.web.connection_tests import (
    _coerce_test_bool, _coerce_test_str, _normalize_test_url, _normalize_abs_test_url, _run_test_connection,
)


from src.services.integration_connection_probes import (
    _test_conn_error, _build_test_url, _test_llm_provider,
    _test_openai_compatible, _test_whispercpp, _test_ollama, _ollama_show_info, _test_abs, _test_kosync, _test_storyteller, _test_booklore, _test_bookorbit, _test_kavita, _test_cwa, _test_hardcover, _test_readest, _test_bookfusion, _test_shelfmark, _test_storygraph, _test_goodreads, _test_telegram,
)



































from src.web.routes.bookfusion import api_bookfusion_device_start, api_bookfusion_device_poll, api_bookfusion_search, api_bookfusion_link, api_bookfusion_unlink, api_bookfusion_upload




from src.web import bookfusion_lookup as _bookfusion_lookup
_bookfusion_lookup._WEB_MODULE = sys.modules[__name__]
from src.web.bookfusion_lookup import (
    _bookfusion_client_for_user,
    _bookfusion_item_matches,
    _bookfusion_reader_id_from_url,
    _find_bookfusion_user_api_id,
    _resolve_created_bookfusion_id,
    _resolve_duplicate_bookfusion_id,
    _serialize_bookfusion_search_item,
)














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


from src.web.routes.diagnostics import api_diagnostics_send_now, api_diagnostics_opt_in, my_reports, api_diagnostics_submissions










# --- Application Factory ---
# Largest request body either server reads. The split-port sync server serves the
# same KoSync blueprint, so the same cap fits every device upload it accepts.
_MAX_REQUEST_BYTES = 8 * 1024 * 1024


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

    app.config["MAX_CONTENT_LENGTH"] = _MAX_REQUEST_BYTES

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
    app.after_request(gzip_html_response)  # registered first, so it runs after the CSRF injection
    app.after_request(inject_csrf_script)

    # Register context processors, jinja globals, etc.
    app.context_processor(inject_global_vars)
    app.jinja_env.globals['csrf_token'] = _ensure_csrf_token
    app.jinja_env.globals['safe_folder_name'] = safe_folder_name
    app.jinja_env.globals['suggestion_source_badge'] = suggestion_source_badge
    app.jinja_env.globals['thumb_cover_src'] = thumb_cover_src
    app.jinja_env.globals['suggestion_bulk_block'] = _suggestion_bulk_block

    # i18n: gettext for the Jinja layer. The selector is passed as an argument
    # (Flask-Babel >= 3.0 dropped the @babel.localeselector decorator); it is
    # called lazily on the first get_locale()/render, so no before_request hook
    # is needed. Flask-Babel is a hard dependency — a missing install must fail
    # loudly here, not leave templates rendering with an undefined `_`.
    import flask_babel

    # Flask-Babel resolves catalogs relative to app.root_path unless told
    # otherwise; point it at our translations dir (missing dir is fine — every
    # string then falls back to English).
    app.config.setdefault('BABEL_TRANSLATION_DIRECTORIES', translations_dir())
    flask_babel.Babel(app, locale_selector=get_locale, default_locale=_BASE_LOCALE)
    # Flask-Babel wires `_`/`gettext`/`ngettext` into Jinja but not
    # `get_locale`, which base.html uses for the <html lang> attribute.
    app.jinja_env.globals['get_locale'] = flask_babel.get_locale

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


    # Register all routes here
    from src.web.routes import login as login_routes
    login_routes.register(app, sys.modules[__name__])
    from src.web.routes import account as account_routes
    account_routes.register(app, sys.modules[__name__])
    from src.web.routes import admin_users as admin_users_routes
    admin_users_routes.register(app, sys.modules[__name__])
    from src.web.routes import library as library_routes
    library_routes.register(app, sys.modules[__name__])
    from src.web.routes import book as book_routes
    book_routes.register(app, sys.modules[__name__])
    from src.web.routes import book_actions as book_actions_routes
    book_actions_routes.register(app, sys.modules[__name__])
    from src.web.routes import processing as processing_routes
    processing_routes.register(app, sys.modules[__name__])
    from src.web.routes import whats_new as whats_new_routes
    whats_new_routes.register(app, sys.modules[__name__])
    from src.web.routes import forge as forge_routes
    forge_routes.register(app, sys.modules[__name__])
    from src.web.routes import matching as matching_routes
    matching_routes.register(app, sys.modules[__name__])
    from src.web.routes import suggestions as suggestions_routes
    suggestions_routes.register(app, sys.modules[__name__])
    from src.web.routes import readalong as readalong_routes
    readalong_routes.register(app, sys.modules[__name__])
    from src.web.routes import me as me_routes
    me_routes.register(app, sys.modules[__name__])
    from src.web.routes import covers as covers_routes
    covers_routes.register(app, sys.modules[__name__])
    from src.web.routes import system as system_routes
    system_routes.register(app, sys.modules[__name__])
    from src.web.routes import status as status_routes
    status_routes.register(app, sys.modules[__name__])
    from src.web.routes import stats as stats_routes
    stats_routes.register(app, sys.modules[__name__])
    from src.web.routes import logs as logs_routes
    logs_routes.register(app, sys.modules[__name__])
    from src.web.routes import settings as settings_routes
    settings_routes.register(app, sys.modules[__name__])

    # Suggestion routes
    from src.api.suggestions_redesign import init_suggestions_redesign_routes
    init_suggestions_redesign_routes(app, sys.modules[__name__])
    from src.api.acquisition_routes import init_acquisition_routes
    init_acquisition_routes(app, sys.modules[__name__])
    from src.web.routes import integration_lookups as integration_lookups_routes
    integration_lookups_routes.register(app, sys.modules[__name__])
    from src.web.routes import bookfusion as bookfusion_routes
    bookfusion_routes.register(app, sys.modules[__name__])
    from src.web.routes import diagnostics as diagnostics_routes
    diagnostics_routes.register(app, sys.modules[__name__])

    # Storyteller API routes
    from src.web.routes import storyteller as storyteller_routes
    storyteller_routes.register(app, sys.modules[__name__])
    from src.web.routes import admin_maintenance as admin_maintenance_routes
    admin_maintenance_routes.register(app, sys.modules[__name__])

    # Forge routes
    from src.web.routes import alignments as alignments_routes
    alignments_routes.register(app, sys.modules[__name__])


    # Return both app and container for external reference
    return app, container


def _log_ebook_source_status() -> None:
    """Log where new matches will find ebooks, or warn that nothing can supply one."""
    # library_roots() ends with the epub cache, which only holds downloaded copies.
    mounted = [root for root in library_roots()[:-1] if root.exists()]
    if container.booklore_client().is_configured():
        logger.info(f"✅ Grimmory integration enabled - ebooks sourced from API")
    elif container.bookorbit_client().is_configured():
        logger.info(f"✅ BookOrbit integration enabled - ebooks sourced from API")
    elif container.kavita_client().is_configured():
        logger.info("Kavita integration enabled - ebooks sourced from API")
    elif mounted:
        logger.info(f"✅ Ebooks directory mounted at {', '.join(str(root) for root in mounted)}")
    else:
        logger.info(
            "⚠️  NO EBOOK SOURCE CONFIGURED: No ebook source available. "
            "New book matches will fail. Enable Grimmory (BOOKLORE_SERVER, BOOKLORE_USER, BOOKLORE_PASSWORD), "
            "enable BookOrbit (BOOKORBIT_SERVER, BOOKORBIT_USER, BOOKORBIT_PASSWORD), "
            "enable Kavita (KAVITA_SERVER, KAVITA_API_KEY), "
            f"or mount the ebooks directory to {library_roots()[0]} (BOOKS_DIR) or an EXTRA_EBOOK_DIRS path."
        )


def create_sync_only_app() -> Flask:
    """The split-port KoSync server: the sync blueprint alone, with the body cap.

    Two KoSync routes read a JSON body before any credential check, so without
    the cap an unauthenticated request of any size is read and parsed in full.
    """
    sync_app = Flask(__name__)
    sync_app.config["MAX_CONTENT_LENGTH"] = _MAX_REQUEST_BYTES
    sync_app.register_blueprint(kosync_sync_bp)

    @sync_app.route('/')
    def sync_health():
        return "Sync Server OK", 200

    return sync_app

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
    threading.Thread(target=_warm_dashboard_sync_warnings, daemon=True, name="dashboard-warmup").start()
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
        container.forge_service().sweep_orphaned_staging()
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

    _log_ebook_source_status()

    logger.info(f"🌐 Web interface starting on port 5757")

    # --- Split-Port Mode ---
    sync_port = os.environ.get('KOSYNC_PORT')
    if sync_port and int(sync_port) != 5757:
        def run_sync_only_server(port):
            create_sync_only_app().run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

        threading.Thread(target=run_sync_only_server, args=(int(sync_port),), daemon=True).start()
        logger.info(f"🚀 Split-Port Mode Active: Sync-only server on port {sync_port}")

    app.run(host='0.0.0.0', port=5757, debug=False)
