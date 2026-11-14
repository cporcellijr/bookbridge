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


# ---------------- i18n / UI LANGUAGE ----------------
# The interface language. Resolution order: the user's saved `users.language`
# (when it names a language we actually have a catalog for), then the request's
# Accept-Language, then English. Wrapped strings and the catalogs are gettext
# (Flask-Babel); only user-facing display text is translated — log messages,
# setting keys and API/JSON field names stay English by design.

# Babel wants `_`-style codes ("ru_RU"); browsers send tags ("ru-RU"). Everything
# is reduced to the primary subtag, which is all we key catalogs and the column on.
_BASE_LOCALE = "en"


def _normalize_locale(value: Optional[str]) -> Optional[str]:
    """'ru-RU' / 'ru_RU' / 'ru' -> 'ru'; junk -> None."""
    if not value:
        return None
    primary = str(value).strip().replace("_", "-").split("-")[0].lower()
    return primary or None


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


def locale_display_name(code: str) -> str:
    """'ru' -> 'Русский' (the language's name in itself); falls back to the code."""
    try:
        from babel import Locale

        return Locale.parse(code).get_display_name(code).capitalize()
    except Exception:
        return code


def get_locale():
    """Flask-Babel locale selector (passed to Babel(locale_selector=...))."""
    try:
        user = current_user()
        if user is not None:
            wanted = _normalize_locale(getattr(user, "language", None))
            if wanted and wanted in available_locales():
                return wanted
    except Exception:
        pass
    try:
        if has_request_context():
            best = request.accept_languages.best_match(available_locales())
            if best:
                return best
    except Exception:
        pass
    return _BASE_LOCALE


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
    def shelfmark_client(self): return container.shelfmark_client()
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


def _scan_user_clients():
    """uc() for Suggestions scans: a user who may not use the admin's clients gets an error, not them."""
    clients = uc()
    if isinstance(clients, _GlobalClients):
        user_id = get_current_user_id()
        user = database_service.get_user(user_id) if user_id is not None and database_service is not None else None
        if user is not None and not _global_fallback_allowed(database_service, user):
            raise PermissionError("user client bundle unavailable")
    return clients


def _acting_user_may_use_global_config() -> bool:
    """True when no user is acting or the acting user may use the admin's global clients."""
    user_id = get_current_user_id()
    if user_id is None or database_service is None:
        return True
    user = database_service.get_user(user_id)
    return user is None or _global_fallback_allowed(database_service, user)


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
            goodreads = sync_clients.get("Goodreads")
            if goodreads and goodreads.is_configured():
                try:
                    goodreads._automatch(book)
                except Exception as e:
                    logger.warning("Deferred Goodreads automatch failed for '%s': %s", abs_id, e, exc_info=True)
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


def _visible_batch_items(items, token):
    """Expose which submitted batch item is being handed off to a worker."""
    for idx, item in enumerate(items):
        with _VISIBLE_BATCH_LOCK:
            if token in _VISIBLE_BATCHES:
                _VISIBLE_BATCHES[token]['current'] = idx
        yield item


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
    batch_token = None
    if label in _VISIBLE_BATCH_LABELS and args and isinstance(args[0], list) and args[0]:
        owner = current_user()
        batch_token = uuid.uuid4().hex
        # Only retain display metadata, never file paths or service credentials.
        safe_items = [
            {key: item.get(key) for key in (
                'abs_id', 'abs_title', 'audio_title', 'title', 'readalong_epub_requested'
            )} for item in args[0]
        ]
        with _VISIBLE_BATCH_LOCK:
            _VISIBLE_BATCHES[batch_token] = {
                'user_id': owner.id if owner else None,
                'label': _VISIBLE_BATCH_LABELS[label],
                'items': safe_items,
                'current': -1,
            }
        args = (_visible_batch_items(args[0], batch_token), *args[1:])

    def finish_visible_batch():
        if batch_token is not None:
            with _VISIBLE_BATCH_LOCK:
                _VISIBLE_BATCHES.pop(batch_token, None)

    if _BACKGROUND_TASKS_SYNCHRONOUS:
        try:
            fn(*args)
        except Exception as e:
            logger.error("%s failed: %s", label, e, exc_info=True)
        finally:
            finish_visible_batch()
        return

    bundle = uc()
    try:
        user = current_user()
        user_id = user.id if user is not None else None
    except Exception:
        user_id = None
    if user_id is None:
        user_id = get_current_user_id()
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
            finish_visible_batch()

    try:
        threading.Thread(target=runner, daemon=True, name=label).start()
    except Exception:
        finish_visible_batch()
        raise


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
    if request.blueprint == 'admin_api':  # bearer-token admin API, own auth
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


def gzip_html_response(response):
    """after_request hook: compress HTML pages for browsers that accept gzip.

    The Library page is about 500 KB of markup and compresses to a tenth of that in a
    few milliseconds, which matters on a phone.
    """
    try:
        if (
            response.status_code != 200
            or response.mimetype != 'text/html'
            or response.direct_passthrough
            or response.is_streamed
            or 'Content-Encoding' in response.headers
            or request.accept_encodings.quality('gzip') <= 0
        ):
            return response
        body = response.get_data()
        if len(body) < 1024:
            return response
        response.set_data(gzip.compress(body, compresslevel=6))
        response.headers['Content-Encoding'] = 'gzip'
        response.vary.add('Accept-Encoding')
    except Exception as e:  # compression must never break a page
        logger.debug(f"Gzip skipped: {e}")
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
    values are rejected to prevent an open redirect, as are control characters:
    browsers drop a tab (`/\t/host` becomes `//host`) and CR/LF break headers."""
    fallback = default or url_for('index')
    next_url = request.args.get('next') or fallback
    if (
        not next_url.startswith('/')
        or next_url.startswith('//')
        or next_url.startswith('/\\')
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in next_url)
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


from src.web.routes.login import login, setup, logout


from src.web.routes.account import account, account_language, account_interface, account_display, account_display_page, account_integrations, account_test_connection, account_abs_libraries, account_booklore_libraries, account_bookorbit_scan_libraries










def _display_settings_for(user) -> dict:
    """Resolved display settings for ``user`` (server defaults when None), cached for the request."""
    cached = g.get('display_settings')
    if cached is None:
        stored = database_service.get_user_credentials(user.id) if user is not None else {}
        cached = g.display_settings = resolve_display_settings(stored or {})
    return cached




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
    values = {}
    for _group, fields in PER_USER_FIELD_GROUPS:
        for key, _label, ftype in fields:
            if ftype == 'bool':
                values[key] = 'true' if key in request.form else 'false'
            elif ftype == 'secret':
                submitted = request.form.get(key, '')
                if submitted:  # blank => keep existing secret
                    values[key] = submitted
            else:  # text/select: blank clears => inherit master/default
                values[key] = request.form.get(key, '')

    destination = values.get('SHELFMARK_AUDIOBOOK_LIBRARY_ID', '')
    saved_destination = str(database_service.get_user_credential(user_id, 'SHELFMARK_AUDIOBOOK_LIBRARY_ID', '') or '')
    if destination:
        if not destination.isdecimal() or int(destination) <= 0:
            return 'Audiobook destination library must be a positive BookOrbit library ID.'
    if destination and destination != saved_destination:
        # Only a newly chosen destination needs the live check; unrelated edits must not depend on BookOrbit.
        target = database_service.get_user(user_id)
        libraries, error = _reader_scan_libraries(target, dict(request.form))
        if error:
            return error
        if not any(str(item.get('id')) == destination for item in libraries):
            return 'Selected audiobook destination library is not accessible for scanning.'
    database_service.set_user_credentials(user_id, values)
    _after_user_integrations_saved(user_id)
    return None


def _after_user_integrations_saved(user_id):
    """Refresh a user's cached clients after their integrations change, and mirror
    the primary admin's library-lookup creds to the global settings."""
    _invalidate_user_clients(user_id)

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

    # The user's ABS Socket.IO listener authenticates with their own key, so a
    # new or rotated key has to reach it: reconnect a running listener, or start
    # one for an account that only now has a key. This runs after the mirror
    # above so the primary admin's token still matches the global key and is
    # not given a second, per-user listener.
    _refresh_user_abs_listener(user_id)


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


_TEST_CONNECTION_FIELDS = {
    'abs': ['ABS_SERVER', 'ABS_KEY'],
    'kosync': [
        'KOSYNC_ENABLED', 'KOSYNC_SERVER', 'KOSYNC_USER', 'KOSYNC_KEY',
        'KOSYNC_AUTH_METHOD',
    ],
    'storyteller': ['STORYTELLER_ENABLED', 'STORYTELLER_API_URL', 'STORYTELLER_USER', 'STORYTELLER_PASSWORD'],
    'booklore': ['BOOKLORE_ENABLED', 'BOOKLORE_SERVER', 'BOOKLORE_USER', 'BOOKLORE_PASSWORD'],
    'bookorbit': ['BOOKORBIT_ENABLED', 'BOOKORBIT_SERVER', 'BOOKORBIT_USER', 'BOOKORBIT_PASSWORD', 'BOOKORBIT_API_KEY'],
    'kavita': ['KAVITA_ENABLED', 'KAVITA_SERVER', 'KAVITA_API_KEY'],
    'bookfusion': ['BOOKFUSION_ENABLED', 'BOOKFUSION_API_URL', 'BOOKFUSION_ACCESS_TOKEN'],
    'cwa': ['CWA_ENABLED', 'CWA_SERVER', 'CWA_USERNAME', 'CWA_PASSWORD', 'CWA_SYNC_TOKEN'],
    'readest': ['READEST_ANNOTATION_SYNC', 'READEST_EMAIL', 'READEST_PASSWORD', 'READEST_SUPABASE_URL'],
    'hardcover': ['HARDCOVER_ENABLED', 'HARDCOVER_TOKEN'],
    'storygraph': ['STORYGRAPH_ENABLED', 'STORYGRAPH_SESSION_COOKIE', 'STORYGRAPH_REMEMBER_USER_TOKEN'],
    'goodreads': ['GOODREADS_ENABLED', 'GOODREADS_SESSION_COOKIE', 'GOODREADS_COOKIE_REFRESH_URL', 'GOODREADS_COOKIE_REFRESH_TOKEN', 'GOODREADS_BROWSER_URL'],
    'shelfmark': ['SHELFMARK_URL', 'SHELFMARK_USER', 'SHELFMARK_PASSWORD'],
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








def _reader_scan_libraries(target, submitted):
    """Read only libraries this reader may actually scan; never use an admin client."""
    from src.api.bookorbit_client import BookOrbitClient
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

    if target is None:
        return [], "Reader not found."
    payload = _posted_user_test_credentials(target, submitted)
    payload[_ALLOW_GLOBAL_FALLBACK_KEY] = _global_fallback_allowed(database_service, target)
    client = BookOrbitClient(credentials=payload)
    if not client.is_configured():
        return [], "Configure this reader's BookOrbit credentials first."
    libraries = client.get_libraries()
    if not isinstance(libraries, list) or not libraries:
        return [], "BookOrbit libraries could not be listed for this reader."
    allowed = []
    first_error = None
    for library in libraries:
        if not isinstance(library, dict) or str(library.get('id') or '').isdigit() is False:
            continue
        _history, error = client.get_scan_history(library['id'], limit=1)
        if error is None:
            allowed.append({'id': str(library['id']), 'name': str(library.get('name') or library['id'])})
        elif first_error is None:
            first_error = error
    if not allowed:
        return [], ('This BookOrbit account needs Manage libraries and library access to start scans.'
                    if first_error == 'scan_forbidden' else
                    'BookOrbit scan permissions or libraries could not be verified.')
    return allowed, None












# User-management actions accepted by both the legacy /admin/users page and the
# Settings → Users tab (which posts to /settings).
# Every action _apply_user_admin_action handles MUST be listed here: POST /settings
# routes anything missing into the settings-save branch instead, which writes the
# whole settings form and restarts.
_USER_ADMIN_ACTIONS = {'create', 'reset_password', 'toggle_active', 'set_role', 'delete', 'share_library'}



def _invalidate_user_clients(user_id: int) -> None:
    """Refresh cached credentials and access policy after a user's account changes."""
    try:
        container.user_client_registry().invalidate(user_id)
    except Exception as e:
        logger.debug("Could not invalidate client bundle for user %s: %s", user_id, e)


def _refresh_user_abs_listener(user_id: int) -> None:
    """Re-evaluate the user's ABS Socket.IO listener after their account or credentials changed."""
    if abs_socket_manager is None:
        return
    try:
        abs_socket_manager.refresh_user(user_id)
    except Exception as e:
        logger.warning("Could not refresh the ABS listener for user %s: %s", user_id, e, exc_info=True)


def _after_user_account_changed(user_id: int) -> None:
    """Apply an enable/disable/role change: refresh cached clients and the user's ABS listener."""
    _invalidate_user_clients(user_id)
    _refresh_user_abs_listener(user_id)


def _share_library_with_new_user(new_user, returning: bool = False) -> int:
    """With share-all-books on, a new or re-enabled account starts with the whole
    library rather than only books matched from now on. Returns the number of
    books shared."""
    if not env_truthy('SHARE_ALL_BOOKS_WITH_ALL_USERS'):
        return 0
    kind = "re-enabled" if returning else "new"
    try:
        linked = database_service.backfill_user_books_for_user(new_user.id)
        logger.info(
            "🔗 Shared %d existing book(s) with %s user '%s' (share-all-books enabled)",
            linked, kind, sanitize_log_data(new_user.username),
        )
        return linked
    except Exception as share_err:
        logger.warning(
            "Could not backfill shared books for %s user '%s': %s",
            kind, sanitize_log_data(new_user.username), share_err, exc_info=True,
        )
        return 0


def _share_library_with_returning_user(user) -> int:
    """Share-on-enable for an account that already existed."""
    return _share_library_with_new_user(user, returning=True)


def _apply_user_admin_action(form):
    """Handle a single user-management action (create/reset/toggle/delete).

    Shared by the legacy /admin/users page and the Settings → Users tab.
    Returns (message, error)."""
    message = None
    error = None

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
                linked = _share_library_with_new_user(new_user)
                if linked:
                    message = f"Created user '{username}' and shared {linked} book(s)"
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
                try:
                    database_service.update_user_account(uid, active=not disabling)
                except ValueError as policy_error:
                    error = str(policy_error)
                else:
                    _after_user_account_changed(uid)
                    message = f"{'Disabled' if disabling else 'Enabled'} '{target.username}'"
                    if not disabling:
                        linked = _share_library_with_returning_user(target)
                        if linked:
                            message = f"Enabled '{target.username}' and shared {linked} book(s)"
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
            else:
                try:
                    database_service.update_user_account(uid, role=new_role)
                except ValueError as policy_error:
                    error = str(policy_error)
                else:
                    # The cached client bundle carries the global-fallback flag, which
                    # this change flips.
                    _after_user_account_changed(uid)
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
            else:
                try:
                    deleted = database_service.delete_user(uid)
                except ValueError as policy_error:
                    error = str(policy_error)
                else:
                    if deleted:
                        _clear_match_queue_for_user_id(uid)
                        _invalidate_user_clients(uid)
                        if abs_socket_manager is not None:
                            abs_socket_manager.stop_user(uid)
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




def ui_template(name: str) -> str:
    """Use a matching New-interface template for this request's reader, if present."""
    if user_setting("UI_MODE", DEFAULT_CONFIG["UI_MODE"]) == "new":
        candidate = f"next/{name}"
        try:
            current_app.jinja_env.get_template(candidate)
        except TemplateNotFound:
            pass
        else:
            return candidate
    return name


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
        display_settings=lambda: _display_settings_for(current_user()),
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
        'goodreads': _flag(lambda: container.goodreads_client().is_configured()),
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


KOSYNC_MIRRORED_LOGIN_KEY = "KOSYNC_MIRRORED_LOGIN"


def _kosync_login_fingerprint(username, password):
    return hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()


def _mirror_kosync_login(user, registry, holders):
    client = registry.get_clients(user.id).booklore_client
    if not client.is_configured():
        return False
    login = client.get_koreader_sync_login()
    if not login:
        return False
    username, password = login
    conditions = get_persistent_condition_logger()
    reader = sanitize_log_data(user.username)

    collision_key = f"kosync_login_mirror_collision:{user.id}"
    holder = holders.get(username.lower())
    if holder is not None and holder.id != user.id:
        conditions.warn(
            logger, collision_key,
            "⚠️ KoSync login for '%s' not taken from Grimmory: the username '%s' already belongs to '%s'",
            reader, sanitize_log_data(username), sanitize_log_data(holder.username),
        )
        return False
    conditions.resolve(logger, collision_key, "KoSync username for '%s' no longer belongs to another user", reader)

    creds = database_service.get_user_credentials(user.id)
    stored_user = creds.get("KOSYNC_USER") or ""
    stored_key = creds.get("KOSYNC_KEY") or ""
    mirrored = creds.get(KOSYNC_MIRRORED_LOGIN_KEY)
    own_login_key = f"kosync_login_mirror_kept:{user.id}"
    if (
        stored_user and stored_key
        and (stored_user, stored_key) != (username, password)
        and mirrored != _kosync_login_fingerprint(stored_user, stored_key)
    ):
        conditions.warn(
            logger, own_login_key,
            "⚠️ KoSync login for '%s' was set in BookBridge and is kept; clear their KoSync username "
            "to take the login from Grimmory",
            reader,
        )
        return False
    conditions.resolve(logger, own_login_key, "KoSync login for '%s' now follows Grimmory", reader)

    changed = []
    if stored_user != username:
        changed.append("username")
    if stored_key != password:
        changed.append("password")
    fingerprint = _kosync_login_fingerprint(username, password)
    if changed or mirrored != fingerprint:
        database_service.set_user_credentials(user.id, {
            "KOSYNC_USER": username,
            "KOSYNC_KEY": password,
            KOSYNC_MIRRORED_LOGIN_KEY: fingerprint,
        })
    if not changed:
        return False
    registry.invalidate(user.id)
    holders[username.lower()] = user
    logger.info("🔑 KoSync login for '%s' taken from Grimmory (%s)", reader, ", ".join(changed))
    return True


def _mirror_kosync_logins_from_grimmory():
    if not env_truthy('KOSYNC_CREDENTIALS_FROM_GRIMMORY') or database_service is None:
        return
    conditions = get_persistent_condition_logger()
    try:
        registry = container.user_client_registry()
        users = database_service.list_users()
        holders = {}
        for user in users:
            name = (database_service.get_user_credentials(user.id).get("KOSYNC_USER") or "").lower()
            if name:
                holders.setdefault(name, user)
        global_name = os.environ.get("KOSYNC_USER", "").lower()
        default_user_id = database_service._default_user_id()
        default_user = next((user for user in users if user.id == default_user_id), None)
        if global_name and default_user is not None:
            holders.setdefault(global_name, default_user)
    except Exception as e:
        conditions.warn(
            logger, "kosync_login_mirror", "KoSync login mirror: could not list users: %s", e, exc_info=True,
        )
        return
    conditions.resolve(logger, "kosync_login_mirror", "KoSync login mirror: users listed again")

    updated = 0
    for user in users:
        if not user.active:
            continue
        failure_key = f"kosync_login_mirror:{user.id}"
        try:
            if _mirror_kosync_login(user, registry, holders):
                updated += 1
        except Exception as e:
            conditions.warn(
                logger, failure_key, "KoSync login mirror failed for user %s: %s", user.id, e, exc_info=True,
            )
        else:
            conditions.resolve(logger, failure_key, "KoSync login mirror works again for user %s", user.id)
    return updated


def sync_daemon():
    """Background sync daemon running in a separate thread."""
    try:
        # Setup schedule for sync operations
        # Use the global SYNC_PERIOD_MINS which is validated
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(manager.run_sync_for_all_users)
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(_mirror_kosync_logins_from_grimmory)
        schedule.every(1).minutes.do(manager.check_pending_jobs)
        schedule.every(1).minutes.do(_suggestions_auto_scan_tick)
        schedule.every(1).minutes.do(manager.flush_reading_sessions_for_all_users)
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(_reconcile_aligned_shelf)
        schedule.every(1).hours.do(_run_diagnostics_send)
        schedule.every(1).hours.do(_run_storyteller_library_hourly)
        schedule.every(int(SYNC_PERIOD_MINS)).minutes.do(_reconcile_abs_collection)

        logger.info(f"🔄 Sync daemon started (period: {SYNC_PERIOD_MINS} minutes)")

        _mirror_kosync_logins_from_grimmory()

        # Run initial sync cycle (per user)
        try:
            manager.run_sync_for_all_users()
        except Exception as e:
            logger.error(f"❌ Initial sync cycle failed: {e}", exc_info=True)

        _reconcile_aligned_shelf()

        # Catch-up diagnostics send on startup (24h guard inside is a no-op when not due)
        try:
            _run_diagnostics_send()
        except Exception:
            pass

        _reconcile_abs_collection()

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


def _is_abs_audio_book(book):
    if (book.sync_mode or '') == 'ebook_only':
        return False
    if (book.audio_source or 'ABS').strip().lower() != 'abs':
        return False
    return not str(book.abs_id).startswith(('booklore:', 'bookorbit:', 'ebook-'))


def _reconcile_abs_collection():
    if not env_truthy('ABS_COLLECTION_RECONCILE') or database_service is None:
        return
    conditions = get_persistent_condition_logger()
    try:
        default_owner = database_service._default_user_id()
        by_owner = {}
        for book in database_service.get_books_by_status('active'):
            if _is_abs_audio_book(book):
                by_owner.setdefault(book.user_id or default_owner, []).append(book.abs_id)
        registry = container.user_client_registry()
    except Exception as e:
        conditions.warn(
            logger, "abs_collection_reconcile", "ABS collection reconcile failed: %s", e, exc_info=True,
        )
        return
    conditions.resolve(logger, "abs_collection_reconcile", "ABS collection reconcile resumed")
    added = 0
    for owner, item_ids in by_owner.items():
        if owner is None:
            continue
        try:
            bundle = registry.get_clients(owner)
            if not bundle.abs_client.is_configured():
                continue
            collection = resolve_setting(bundle.credentials, "ABS_COLLECTION_NAME", "Synced with KOReader")
            count, refused = bundle.abs_client.add_missing_to_collection(item_ids, collection)
        except Exception as e:
            conditions.warn(
                logger, f"abs_collection_reconcile:{owner}",
                "ABS collection reconcile failed for user %s: %s", owner, e, exc_info=True,
            )
            continue
        conditions.resolve(
            logger, f"abs_collection_reconcile:{owner}", "ABS collection reconcile resumed for user %s", owner,
        )
        added += count
        refused_key = f"abs_collection_refused:{owner}:{collection}"
        if refused:
            conditions.warn(
                logger, refused_key,
                "ABS collection '%s' of user %s is missing %d matched audiobook(s) that could not be added: %s",
                collection, owner, len(refused), refused[0],
            )
        else:
            conditions.resolve(
                logger, refused_key, "ABS collection '%s' of user %s is complete again", collection, owner,
            )
    if added:
        logger.info("🏷️ ABS collection reconcile added %d matched audiobook(s)", added)
    return added


# ---------------- ORIGINAL ABS-KOSYNC HELPERS ----------------

def find_ebook_file(filename):
    if not is_plain_basename(filename):
        path = contained_library_file([EBOOK_DIR], filename)
        if path:
            return path
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
    # A regular user's selection hashes through their own provider, never the server's
    # ebook folder: a submitted path, filename or provider label is not proof of access.
    local_folder_allowed = not acting_user_is_regular(database_service)
    source_path_text = (
        _safe_local_source_path(source_path)
        if local_folder_allowed and "://" not in str(source_path or "") else ""
    )
    if source_path_text:
        try:
            selected_path = Path(source_path_text)
            if selected_path.exists() and selected_path.is_file():
                ebook_path = selected_path
            else:
                logger.debug(f"Selected ebook source path not found, falling back to filename search: '{source_path_text}'")
        except Exception as e:
            logger.debug(f"Selected ebook source path could not be used, falling back to filename search: {e}")
    if not ebook_path and local_folder_allowed:
        ebook_path = find_ebook_file(ebook_filename)
    if not ebook_path and original_filename and local_folder_allowed:
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

                     # Priority 2: Use direct download URL from search if available
                     if target and target.get('download_url'):
                         logger.info(f"🚀 Using direct download link from search for '{target.get('title', 'Unknown')}'")
                     else:
                         # Priority 3: Fallback to get_book_by_id only if search didn't provide a URL
                         # This may crash server on metadata page, but includes a blind URL fallback
                         if results and not target:
                             logger.info(f"🔍 CWA search for ID '{cwa_id}' returned {len(results)} result(s), none with that ID — looking it up by ID")
                         else:
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


def _aligned_shelf_target(user_id):
    client, shelf, _ = _aligned_shelf_assignment(user_id)
    return client, shelf


def _aligned_shelf_assignment(user_id):
    global_shelf = (os.environ.get('BOOKLORE_SHELF_NAME') or 'Kobo').strip()
    owner = (os.environ.get('BOOKLORE_SHELF_OWNER') or '').strip()
    if owner:
        user = database_service.get_user_by_username(owner)
        if user is None or not user.active:
            get_persistent_condition_logger().warn(
                logger,
                "grimmory_shelf_owner_inactive",
                "⚠️ Grimmory shelf owner '%s' is not an active BookBridge user — "
                "the shelf is left alone until it is",
                sanitize_log_data(owner),
            )
            return None, None, None
        get_persistent_condition_logger().resolve(
            logger,
            "grimmory_shelf_owner_inactive",
            "✅ Grimmory shelf owner '%s' is an active BookBridge user again",
            sanitize_log_data(owner),
        )
        return container.user_client_registry().get_clients(user.id).booklore_client, global_shelf, user.id
    if user_id is None:
        return _global_clients.booklore_client, global_shelf, None
    bundle = container.user_client_registry().get_clients(user_id)
    return (bundle.booklore_client,
            resolve_setting(bundle.credentials, 'BOOKLORE_SHELF_NAME', 'Kobo'), user_id)


def _reconcile_aligned_shelf():
    if database_service is None:
        return
    try:
        pending = database_service.get_pending_shelf_adds()
        if not pending:
            get_persistent_condition_logger().resolve(
                logger, "aligned_shelf_reconcile", "Aligned-shelf reconcile recovered"
            )
            return
        required = env_truthy('BOOKLORE_SHELF_REQUIRE_ALIGNMENT')
        aligned = database_service.get_readalong_alignment_book_ids() if required else set()
        users = {user.id: user for user in database_service.list_users()}
        added = 0
        for abs_id, user_id in pending:
            if required and abs_id not in aligned:
                continue
            book = database_service.get_book(abs_id)
            reader = users.get(user_id)
            if (
                book is None
                or not is_grimmory_source(book.ebook_source)
                or not book.ebook_source_id
                or (user_id is not None and reader is None)
            ):
                database_service.remove_pending_shelf_adds(abs_id, user_id)
                continue
            if (required and book.status != 'active') or (reader is not None and not reader.active):
                continue
            client, shelf, target_user_id = _aligned_shelf_assignment(user_id)
            if client is None or not client.is_configured():
                continue
            if client.add_book_id_to_shelf(book.ebook_source_id, shelf):
                database_service.record_shelf_assignment(
                    abs_id, user_id, target_user_id, shelf, book.ebook_source_id
                )
                database_service.remove_pending_shelf_adds(abs_id, user_id)
                added += 1
                logger.info("🏷️ '%s' added to Grimmory shelf '%s'",
                            sanitize_log_data(book.abs_title or book.abs_id), shelf)
        get_persistent_condition_logger().resolve(
            logger, "aligned_shelf_reconcile", "Aligned-shelf reconcile recovered"
        )
        return added
    except Exception as e:
        get_persistent_condition_logger().warn(
            logger, "aligned_shelf_reconcile",
            "Aligned-shelf reconcile failed: %s", e, exc_info=True,
        )


def _shelve_matched_ebook(shelf_filename, ebook_source=None, ebook_source_id=None, book=None):
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
        elif defer_shelf_add(database_service, book, get_current_user_id()):
            added = True
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
        book=book,
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

    if target_path.exists():
        # This uuid's own earlier artifact (downloads are staged and swapped in, so
        # a failed one never leaves a partial file). The title-matched local copy
        # below must never replace it: the title comes from the caller.
        logger.warning(f"Storyteller local fallback used: '{target_path}'")
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


def _mapping_identity_changes(
    target_book,
    *,
    kosync_doc_id=None,
    ebook_filename=None,
    audio_source_id=None,
    storyteller_uuid=None,
    ebook_source=None,
    ebook_source_id=None,
) -> tuple[list, bool]:
    """Which pairing fields a re-match would change on ``target_book``.

    Returns ``(changed, same_stable_ebook)``. A field going from empty to populated
    is not a change. Shared by the status guard below and the cross-user guard.
    """
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
    return changed, same_stable_ebook


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
    changed, same_stable_ebook = _mapping_identity_changes(
        target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=ebook_filename,
        audio_source_id=audio_source_id,
        storyteller_uuid=storyteller_uuid,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
    )

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
    """Link a hash KOReader already knows to a freshly mapped book (#431), never re-pointing.

    One atomic statement leaves a hash another book owns, by link or as its primary,
    with that book. The consolidating paths (match, absorb, ebook-only tri-link)
    delete the loser first, so they reach this with nothing to conflict against.
    """
    if not abs_id or not isinstance(kosync_doc_id, str) or not kosync_doc_id.strip():
        return
    try:
        doc_id = kosync_doc_id.strip()
        if database_service.adopt_kosync_document_if_unowned(doc_id, abs_id):
            logger.info(
                "🔗 Adopted existing KoSync document '%s' for '%s'",
                sanitize_log_data(kosync_doc_id), sanitize_log_data(abs_id),
            )
            return
        existing = database_service.get_kosync_document(doc_id)
        owner = getattr(existing, "linked_abs_id", None) if existing else None
        if not owner:
            primary = database_service.get_book_by_kosync_id(doc_id)
            owner = getattr(primary, "abs_id", None) if primary else None
        if owner and owner != abs_id:
            logger.info(
                "🔒 KoSync document '%s' already belongs to '%s' — not re-pointing it to '%s'",
                sanitize_log_data(doc_id), sanitize_log_data(owner), sanitize_log_data(abs_id),
            )
    except Exception as e:
        logger.warning(f"⚠️ Failed to adopt KoSync document for '{abs_id}': {e}", exc_info=True)


def _storyteller_register_file_hashes(storyteller_uuid, abs_id):
    """Register Storyteller OPDS hashes off the match request thread."""
    from src.services.storyteller_library_watch import register_hashes

    try:
        book = database_service.get_book(abs_id)
        if not book or book.storyteller_uuid != storyteller_uuid:
            logger.info("Storyteller hashes: stale mapping %s; skipping", abs_id)
            return
        user_id = get_current_user_id()
        client = (container.user_client_registry().get_clients(user_id).storyteller_client
                  if user_id is not None else uc().storyteller_client)
        register_hashes(
            database_service, client, book,
            user_id=user_id,
        )
        get_persistent_condition_logger().resolve(
            logger, f"storyteller-registration:{user_id}:{storyteller_uuid}",
            "Storyteller hashes: registration recovered for %s", storyteller_uuid)
    except Exception as exc:
        get_persistent_condition_logger().warn(
            logger, f"storyteller-registration:{get_current_user_id()}:{storyteller_uuid}",
            "Storyteller hashes: registration failed for %s: %s", storyteller_uuid, exc, exc_info=True)


def _storyteller_auto_link_candidates(user_id, client, library, books, claimed, deadline=None):
    """Auto-link aligned books with bounded joins and round-robin attempts."""
    from unicodedata import normalize
    from src.services.storyteller_library_watch import _STATE_LOCK, state_key

    enabled = str(user_setting("STORYTELLER_AUTO_LINK", "false") or "").strip().lower()
    if enabled not in {"true", "on", "1", "yes"} or not library:
        return 0
    if deadline is None:
        deadline = time.monotonic() + 600

    def title_key(text):
        return re.sub(r"[\W_]+", "", normalize("NFKC", str(text or "")).casefold())

    key = state_key(user_id)
    try:
        state = database_service.get_json_setting(key, default={})
    except Exception:
        state = {}
    state = state if isinstance(state, dict) else {}
    skipped = state.get("_auto_skip", {})
    skipped = dict(skipped) if isinstance(skipped, dict) else {}
    skipped_before = dict(skipped)
    start = int(state.get("_auto_cursor", 0) or 0) % len(library)
    inspected = linked = joined = 0
    conditions = get_persistent_condition_logger()
    now = time.time()
    for index in range(len(library)):
        if time.monotonic() >= deadline:
            logger.info("Storyteller auto-link: user %s reached pass time budget; stopping", user_id)
            break
        item = library[(start + index) % len(library)]
        inspected += 1
        if not isinstance(item, dict):
            continue
        uuid_value = str(item.get("uuid") or item.get("id") or "").strip()
        readaloud = item.get("readaloud") or {}
        if not uuid_value or not isinstance(readaloud, dict):
            continue
        if str(readaloud.get("status") or "").upper() != "ALIGNED" or readaloud.get("missing"):
            logger.debug("Storyteller auto-link: %s not aligned; skipping", uuid_value)
            continue
        fingerprint = str(readaloud.get("fingerprint") or "")
        failure_key = f"storyteller-auto-link:{user_id}:{uuid_value}"
        failed = False
        try:
            already = next((book for book in books if book.storyteller_uuid == uuid_value), None)
            if already:
                if claimed is not None and already.abs_id not in claimed:
                    if joined >= 25:
                        continue
                    access = _existing_mapping_access(
                        already, storyteller_uuid=uuid_value,
                        ebook_filename=already.ebook_filename,
                        kosync_doc_id=already.kosync_doc_id,
                    )
                    if access in {"join", "modify"}:
                        _claim_book_for_user_id(user_id, already.abs_id)
                        claimed.add(already.abs_id)
                        joined += 1
                        skipped.pop(uuid_value, None)
                        logger.info("Storyteller auto-link: joined existing %s for user %s",
                                    uuid_value, user_id)
                else:
                    logger.debug("Storyteller auto-link: %s already mapped; skipping", uuid_value)
                continue

            decline = skipped.get(uuid_value)
            if (isinstance(decline, dict) and decline.get("fingerprint") == fingerprint
                    and isinstance(decline.get("ts"), (int, float))
                    and 0 <= now - decline["ts"] < 7 * 24 * 60 * 60):
                logger.debug("Storyteller auto-link: %s declined earlier; skipping", uuid_value)
                continue
            if "ebook" in item and not item["ebook"]:
                logger.debug("Storyteller auto-link: %s has no plain EPUB; skipping", uuid_value)
                continue
            plain_hash = client.get_kosync_content_hash(uuid_value, "ebook")
            if not isinstance(plain_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", plain_hash):
                logger.info("Storyteller auto-link: plain hash unavailable for %s", uuid_value)
                continue
            existing = database_service.get_book_by_kosync_id(plain_hash)
            if not existing:
                doc = database_service.get_kosync_document(plain_hash)
                existing = (database_service.get_book(doc.linked_abs_id)
                            if doc and getattr(doc, "linked_abs_id", None) else None)
            if existing:
                access = _existing_mapping_access(
                    existing, storyteller_uuid=uuid_value,
                    ebook_filename=existing.ebook_filename,
                    kosync_doc_id=existing.kosync_doc_id,
                )
                if access == "deny":
                    skipped[uuid_value] = {"fingerprint": fingerprint, "ts": now}
                    logger.info("Storyteller auto-link: %s belongs to another reader; skipping", uuid_value)
                    continue
                if access == "join":
                    if joined >= 25:
                        continue
                    _claim_book_for_user_id(user_id, existing.abs_id)
                    if claimed is not None:
                        claimed.add(existing.abs_id)
                    joined += 1
                    skipped.pop(uuid_value, None)
                    logger.info("Storyteller auto-link: joined existing %s for user %s",
                                uuid_value, user_id)
                    continue
                saved, error, _status = _upsert_storyteller_mapping(
                    mode_hint="existing", existing_book=existing,
                    abs_id=existing.abs_id, abs_title=existing.abs_title,
                    storyteller_uuid=uuid_value, preserve_primary_kosync=True,
                )
                if error or not saved:
                    logger.info("Storyteller auto-link: attach failed for %s: %s", uuid_value, error)
                    continue
            else:
                title = str(item.get("title") or "").strip()
                authors = item.get("authors") or []
                author_names = ", ".join(
                    author.get("name", "") if isinstance(author, dict) else str(author)
                    for author in authors
                ) if isinstance(authors, list) else str(authors)
                duplicate = False
                for book in books:
                    if title_key(book.abs_title) != title_key(title):
                        continue
                    # Older Book mappings have no author field. Fail closed on
                    # their shared title rather than risk replacing an audio mapping.
                    mapped_author = (getattr(book, "audio_author", None)
                                     or getattr(book, "author", None) or "")
                    if not mapped_author or not author_names or title_key(mapped_author) == title_key(author_names):
                        duplicate = True
                        break
                if duplicate:
                    skipped[uuid_value] = {"fingerprint": fingerprint, "ts": now}
                    logger.info("Storyteller auto-link: '%s' title/author collides; use Add Book", title)
                    continue
                saved = _create_ebook_only_mapping_from_queue_item({
                    "storyteller_uuid": uuid_value,
                    "abs_title": title,
                })
                if not saved:
                    logger.info("Storyteller auto-link: create failed for %s", uuid_value)
                    continue

            _claim_book_for_user_id(user_id, saved.abs_id)
            if claimed is not None:
                claimed.add(saved.abs_id)
            books.append(saved)
            skipped.pop(uuid_value, None)
            linked += 1
            logger.info("Storyteller auto-link: linked %s to %s for user %s",
                        uuid_value, saved.abs_id, user_id)
        except Exception as exc:
            failed = True
            conditions.warn(logger, failure_key, "Storyteller auto-link: failed for %s: %s",
                            uuid_value, exc, exc_info=True)
        finally:
            if not failed:
                conditions.resolve(logger, failure_key, "Storyteller auto-link: recovered for %s", uuid_value)
    cursor = (start + inspected) % len(library)
    if skipped != skipped_before or cursor != state.get("_auto_cursor", 0):
        failure_key = f"storyteller-auto-state:{user_id}"
        try:
            with _STATE_LOCK:
                current = database_service.get_json_setting(key, default={})
                current = current if isinstance(current, dict) else {}
                current["_auto_skip"] = skipped
                current["_auto_cursor"] = cursor
                database_service.set_json_setting(key, current)
            conditions.resolve(logger, failure_key, "Storyteller auto-link: declined books saved for user %s", user_id)
        except Exception as exc:
            conditions.warn(logger, failure_key, "Storyteller auto-link: could not save declined books: %s",
                            exc, exc_info=True)
    return linked


_STORYTELLER_PASS_LOCK = threading.Lock()
_STORYTELLER_PASSES_IN_PROGRESS = set()


def _storyteller_user_library_pass(user_id):
    """Reconcile this reader once at a time; stop starting books after ten minutes."""
    from src.services.storyteller_library_watch import scan_linked

    with _STORYTELLER_PASS_LOCK:
        if user_id in _STORYTELLER_PASSES_IN_PROGRESS:
            logger.info("Storyteller hashes: user %s already scanning; skipping", user_id)
            return
        _STORYTELLER_PASSES_IN_PROGRESS.add(user_id)
    conditions = get_persistent_condition_logger()
    failure_key = f"storyteller-library-pass:{user_id}"
    deadline = time.monotonic() + 600
    try:
        client = uc().storyteller_client
        if not client or not client.is_configured():
            return
        library = client.list_library_books()
        if library is None:
            logger.info("Storyteller auto-link: library unavailable for user %s", user_id)
            return
        claimed = (
            set(database_service.get_linked_abs_ids(user_id) or [])
            if user_id is not None else None
        )
        all_books = list(database_service.get_all_books())
        _storyteller_auto_link_candidates(user_id, client, library, all_books, claimed, deadline=deadline)
        books = [
            book for book in all_books
            if book.storyteller_uuid and (claimed is None or book.abs_id in claimed)
        ]
        # Add Book used to save a Storyteller-only book under the placeholder 'Ebook'.
        titles = {
            str(item.get("uuid") or item.get("id")): str(item.get("title") or "").strip()
            for item in library if isinstance(item, dict)
        }
        for book in books:
            title = titles.get(book.storyteller_uuid)
            if getattr(book, "abs_title", None) == "Ebook" and title:
                fields = {"abs_title": title}
                if getattr(book, "audio_title", None) == "Ebook":
                    fields["audio_title"] = title
                if database_service.update_book_fields(book.abs_id, **fields):
                    book.abs_title = title
                    logger.info("Storyteller: renamed '%s' from 'Ebook' to its Storyteller title '%s'",
                                book.abs_id, title)
        scan_linked(database_service, client, books, user_id, library=library, deadline=deadline)
        conditions.resolve(logger, failure_key, "Storyteller hashes: pass recovered for user %s", user_id)
    except Exception as exc:
        conditions.warn(logger, failure_key, "Storyteller hashes: pass failed for user %s: %s",
                        user_id, exc, exc_info=True)
    finally:
        with _STORYTELLER_PASS_LOCK:
            _STORYTELLER_PASSES_IN_PROGRESS.discard(user_id)


def _run_storyteller_library_hourly():
    """Dispatch per-user scans via the existing scoped background helper."""
    from src.utils.user_config import (
        _ALLOW_GLOBAL_FALLBACK_KEY, PER_USER_CREDENTIAL_KEYS, global_fallback_allowed,
    )

    try:
        users = [user for user in database_service.list_users() if getattr(user, "active", 1)]
    except Exception as exc:
        get_persistent_condition_logger().warn(
            logger, "storyteller-library-users", "Storyteller hashes: could not list users: %s", exc, exc_info=True)
        return
    get_persistent_condition_logger().resolve(
        logger, "storyteller-library-users", "Storyteller hashes: user list recovered")
    for user in users:
        uid_token = creds_token = bundle_token = None
        try:
            # Fail closed: uc() otherwise falls back to global clients when
            # a reader's registry bundle is unavailable.
            bundle = container.user_client_registry().get_clients(user.id)
            client = getattr(bundle, "storyteller_client", None)
            if not client or not client.is_configured():
                continue
            bundle_token = _active_bundle.set(bundle)
            uid_token = set_current_user_id(user.id)
            credentials = database_service.get_user_credentials(user.id) or {}
            credentials = {key: val for key, val in credentials.items()
                           if key in PER_USER_CREDENTIAL_KEYS}
            credentials[_ALLOW_GLOBAL_FALLBACK_KEY] = global_fallback_allowed(database_service, user)
            creds_token = set_current_user_credentials(credentials)
            _spawn_user_background(
                _storyteller_user_library_pass, user.id,
                label=f"storyteller-library-{user.id}",
            )
            get_persistent_condition_logger().resolve(
                logger, f"storyteller-library-schedule:{user.id}",
                "Storyteller hashes: scheduling recovered for user %s", user.id)
        except Exception as exc:
            get_persistent_condition_logger().warn(
                logger, f"storyteller-library-schedule:{user.id}",
                "Storyteller hashes: cannot schedule user %s: %s", user.id, exc, exc_info=True)
        finally:
            if creds_token is not None:
                reset_current_user_credentials(creds_token)
            if uid_token is not None:
                reset_current_user_id(uid_token)
            if bundle_token is not None:
                _active_bundle.reset(bundle_token)


EBOOK_ALREADY_MAPPED = "This ebook is already in BookBridge"


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
    preserve_primary_kosync=False,
    precomputed_kosync_doc_id=None,
    ebook_source_path=None,
    create_only=False,
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
    selected_ebook_source_id = str(ebook_source_id).strip() if ebook_source_id is not None else None
    requested_abs_id = str(abs_id or "").strip() or None
    if selected_ebook_source == "BookFusion":
        return None, "BookFusion links require an audiobook mapping", 400

    target_book = existing_book
    if mode_hint == "existing":
        if target_book is None and requested_abs_id:
            target_book = database_service.get_book(requested_abs_id)
        if not target_book:
            return None, "Book not found", 404
        if preserve_primary_kosync and not (target_book.kosync_doc_id and target_book.ebook_filename):
            return None, "Existing book has no primary KoSync EPUB to preserve", 400

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

    kosync_doc_id = precomputed_kosync_doc_id
    if preserve_primary_kosync:
        kosync_doc_id = target_book.kosync_doc_id
        resolved_ebook_filename = target_book.ebook_filename
    elif selected_storyteller_uuid:
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
    elif not kosync_doc_id:
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
            source_path=ebook_source_path,
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
        if create_only and (
            existing_by_hash
            or database_service.get_book(preferred_abs_id or f"ebook-{kosync_doc_id[:16]}")
        ):
            return None, EBOOK_ALREADY_MAPPED, 409

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

    if not _may_absorb_mapping(migration_source_id):
        return None, _FOREIGN_MAPPING_MESSAGE, 403
    access = _existing_mapping_access(
        None if created_ebook_only else target_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        storyteller_uuid=selected_storyteller_uuid,
        ebook_source=selected_ebook_source,
        ebook_source_id=selected_ebook_source_id,
    )
    if access == "join":
        return target_book, None, None
    if access == "deny":
        return None, _FOREIGN_MAPPING_MESSAGE, 403

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

    if create_only:
        # Insert-only: a row another match created after the checks above is left alone.
        saved_book = database_service.save_book(target_book, create_only=True)
        if saved_book is None:
            return None, EBOOK_ALREADY_MAPPED, 409
    else:
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

    if selected_storyteller_uuid:
        _spawn_user_background(
            _storyteller_register_file_hashes, selected_storyteller_uuid, saved_book.abs_id,
            label="storyteller-link-hashes",
        )
    return saved_book, None, None


class EbookResult:
    """Provider or local ebook metadata shared by search and suggestions scans."""

    def __init__(
        self, name, title=None, subtitle=None, authors=None, booklore_id=None,
        path=None, source=None, source_id=None, abs_identifier=None, language=None,
        series_name=None, series_sequence=None, source_file_id=None,
        ebook_file_id=None, file_mtime_ns=None, file_size=None,
    ):
        self.name = name
        self.title = title or Path(name).stem
        self.subtitle = subtitle or ''
        self.authors = authors or ''
        self.language = str(language or '').strip()
        self.booklore_id = booklore_id
        self.path = path
        self.source = source
        self.source_id = source_id if source_id is not None else booklore_id
        self.abs_identifier = abs_identifier
        self.series_name = series_name
        self.series_sequence = series_sequence
        self.source_file_id = source_file_id
        self.ebook_file_id = ebook_file_id
        self.file_mtime_ns = file_mtime_ns
        self.file_size = file_size
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


def _record_suggestions_source_state(source_states, source, role, configured, supported=True, complete=None, count=None, error_type=None):
    source = "Grimmory" if source == "BookLore" else source
    state = source_states.setdefault(source, {"roles": {}})
    state["roles"][role] = {
        "configured": bool(configured),
        "supported": bool(supported),
        "complete": complete,
        "count": count,
        "error_type": error_type,
    }


def get_searchable_audiobooks(search_term, source_states=None, abs_catalog=None, full_refresh=False):
    """Get audiobook results from configured providers, optionally tracking scan health."""
    discovery = source_states is not None
    adapters = {}
    try:
        clients = _scan_user_clients()
        if clients.abs_client and clients.abs_client.is_configured():
            if discovery and abs_catalog is None:
                _record_suggestions_source_state(
                    source_states, "ABS", "audio", True, complete=False, error_type="CatalogUnavailable",
                )
            else:
                adapters["ABS"] = ABSAudioSourceAdapter(
                    clients.abs_client, container.data_dir(),
                    catalog_items=abs_catalog if discovery else None,
                )
        if clients.booklore_client and clients.booklore_client.is_configured():
            catalog = clients.booklore_client.get_all_books(include_all_ids=True, force_refresh=full_refresh) if discovery else None
            adapters["BookLore"] = BookLoreAudioSourceAdapter(
                clients.booklore_client, container.data_dir(), catalog_items=catalog,
            )
        _bo_client = getattr(clients, "bookorbit_client", None)
        if _bo_client and _bo_client.is_configured():
            if full_refresh:
                _bo_client.get_all_books(force_refresh=True)
            adapters["BookOrbit"] = BookOrbitAudioSourceAdapter(_bo_client, container.data_dir())
    except Exception as e:
        if discovery:
            _record_suggestions_source_state(
                source_states, "_client_registry", "audio", True, complete=False, error_type=type(e).__name__,
            )
            logger.warning("Could not build user-scoped audio adapters (%s)", type(e).__name__, exc_info=True)
            return []
        if not _acting_user_may_use_global_config():
            logger.warning(
                "Could not build user-scoped audio adapters; global adapters withheld (%s)",
                type(e).__name__, exc_info=True,
            )
            return []
        logger.debug("Could not build user-scoped audio adapters, falling back to globals: %s", e)
        adapters = container.audio_source_adapters() if hasattr(container, "audio_source_adapters") else {}
    results = []
    seen = set()
    per_adapter_counts = {}

    for source_name, adapter in adapters.items():
        try:
            provider_results = adapter.search(search_term)
        except Exception as e:
            logger.warning(
                "⚠️ Audiobook search failed for %s (%s)", source_name, type(e).__name__,
                exc_info=not discovery,
            )
            per_adapter_counts[source_name] = f"error:{type(e).__name__}"
            if discovery:
                _record_suggestions_source_state(
                    source_states, source_name, "audio", True, complete=False, error_type=type(e).__name__,
                )
            continue
        per_adapter_counts[source_name] = len(provider_results) if provider_results else 0
        if discovery:
            if source_name == "ABS":
                complete = abs_catalog is not None and not getattr(adapter, "catalog_incomplete", False)
            elif source_name == "BookLore":
                complete = bool(getattr(clients.booklore_client, "catalog_complete", False))
            else:
                complete = bool(getattr(clients.bookorbit_client, "catalog_complete", False))
            _record_suggestions_source_state(
                source_states, source_name, "audio", True, complete=complete,
                count=len(provider_results or []),
                error_type=None if complete else "CatalogIncomplete",
            )

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
    if discovery:
        for source_name, client in (
            ("BookLore", clients.booklore_client),
            ("BookOrbit", getattr(clients, "bookorbit_client", None)),
        ):
            state = source_states.get("Grimmory" if source_name == "BookLore" else source_name, {}).get("roles", {}).get("audio", {})
            if not state.get("configured") or state.get("complete") is not True:
                continue
            try:
                if source_name == "BookLore":
                    books = client.get_all_books(include_all_ids=True)
                    state["unknown_companion_count"] = sum(
                        bool(book.get("_needs_detail") and not client._book_supports_audiobook(book)
                             and not client._has_audio_shape_fields(book))
                        for book in books
                    )
                    missing_title = any(
                        (client._book_supports_audiobook(book) or (
                            book.get("_needs_detail") and not client._has_audio_shape_fields(book)
                        )) and not (book.get("title") or book.get("fileName"))
                        for book in books
                    )
                else:
                    books = client.get_all_books()
                    missing_title = any(
                        client._info_offers_kind(book, "audiobook") and not book.get("title")
                        for book in books
                    )
                if missing_title:
                    _record_suggestions_source_state(
                        source_states, source_name, "audio", True, complete=False,
                        error_type="MissingAudioTitle",
                    )
            except Exception as e:
                _record_suggestions_source_state(
                    source_states, source_name, "audio", True, complete=False, error_type=type(e).__name__,
                )
    return results


def _punctuation_search_key(value):
    """Compare title text without punctuation or whitespace."""
    return ''.join(char.lower() for char in (value or '') if char.isalnum())


def _punctuation_search_fragment(term):
    """Return a short phrase shared by punctuated and plain long titles."""
    if ' - ' in (term or ''):
        return ''
    words = re.findall(r'[^\W\d_]+', term or '')
    if words and words[0].lower() in ('the', 'a', 'an'):
        words = words[1:]
    return ' '.join(words[:2]) if len(words) >= 4 else ''


def _audiobook_search_variants(term):
    """Progressive query relaxations for a (possibly filename-derived) term.

    Yields the raw term, then removes filename suffixes and punctuation.
    Long titles also get a short phrase so strict provider searches can find
    editions that differ by punctuation within the title.
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
    search_title = no_edition.split(' - ')[0]
    _add(' '.join(re.findall(r'\w+', search_title)))
    if ' - ' not in no_edition:
        _add(_punctuation_search_fragment(search_title))
    return variants


def _search_audiobooks_with_fallback(term):
    """Merge audiobook hits across title spellings and source catalogs."""
    results = []
    seen = set()
    fragment = _punctuation_search_fragment(term)
    query_key = _punctuation_search_key(term)
    for index, variant in enumerate(_audiobook_search_variants(term)):
        for result in get_searchable_audiobooks(variant):
            if (fragment and variant == fragment and variant != term
                    and query_key not in _punctuation_search_key(result.title)):
                continue
            key = (result.source, result.source_id)
            if key not in seen:
                seen.add(key)
                results.append(result)
                if index > 0:
                    logger.debug("Audiobook search matched on relaxed term %r (from %r)", variant, term)
    results.sort(key=lambda item: (item.title or item.display_name or '').lower())
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
    fragment = _punctuation_search_fragment(term)
    query_key = _punctuation_search_key(term)
    for variant in _audiobook_search_variants(term):
        for ebook in get_searchable_ebooks(variant):
            if (fragment and variant == fragment and variant != term
                    and query_key not in _punctuation_search_key(
                        getattr(ebook, 'title', None) or ebook.name)):
                continue
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


def get_suggestion_audiobooks(source_states=None, abs_catalog=None, full_refresh=False):
    """Return provider-normalized audiobook records for suggestions scan."""
    records = []
    for item in get_searchable_audiobooks(
        "", source_states=source_states, abs_catalog=abs_catalog, full_refresh=full_refresh,
    ):
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
                "audio_subtitle": (item.subtitle or "").strip(),
                "audio_series": (item.series_label or "").strip(),
                "audio_series_name": item.series_name,
                "audio_series_sequence": item.series_sequence,
                "audio_series_source": item.series_source,
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


def _first_not_none(*values):
    return next((value for value in values if value is not None), None)


def _ebook_title_key(value):
    """Normalize a title/filename-stem to an alphanumeric key for joining."""
    return re.sub(r'[\W_]+', '', (value or '').lower())


def _scan_excludes_local_files() -> bool:
    """Scans omit the server's local folder when the admin setting is on or a regular user is acting."""
    return suggestions_exclude_local_files(database_service)


def _submitted_ebook_path(raw_path) -> Optional[str]:
    """A request's ebook path, dropped when it is a regular user's server path.

    Their selections resolve through their own provider, so a submitted server path
    (which a forged provider label could point at the local folder) is never trusted.
    A provider download URL is kept.
    """
    path = str(raw_path or "").strip() or None
    if path and "://" not in path and acting_user_is_regular(database_service):
        return None
    return path


def _refuse_regular_user_local_selection(
    ebook_source, ebook_source_path=None, ebook_source_id=None, ebook_filename=None,
) -> bool:
    """True (and logged) when a regular user submits a server-local ebook selection."""
    if not acting_user_is_regular(database_service):
        return False
    source = _normalize_text_source_type(ebook_source)
    path = str(ebook_source_path or "").strip()
    # A provider entry (BookOrbit, Grimmory...) carries its own server path; only a
    # selection with no provider behind it is the server's local folder.
    local = source == "Local File" or (
        not source and not ebook_source_id and (bool(ebook_filename) or (bool(path) and "://" not in path))
    )
    if local:
        logger.info(
            "🔒 Refused a server-local ebook selection from a non-admin user (%s)",
            sanitize_log_data(ebook_filename or path or "no filename"),
        )
    return local


def _build_local_ebook_title_index(scan_metadata=None):
    """Map normalized title -> filename for every epub on the local /books disk.

    BookBridge and BookOrbit share the same files, so this lets us pair
    BookOrbit's clean metadata (title/author, no filename) with real filenames
    without a per-book BookOrbit detail call. Indexes the full stem, the 'Title'
    portion before ' - ' ('Title - Author (year).epub'), and the title in the
    file's own metadata, which BookOrbit's title comes from whatever the naming
    scheme ('Author - Series 15 - Title.epub', #549)."""
    index = {}
    try:
        if EBOOK_DIR.exists():
            for eb in EBOOK_DIR.rglob('*'):
                if eb.suffix.lower() not in LINKABLE_EBOOK_EXTENSIONS or not eb.is_file():
                    continue
                if is_generated_readalong_epub(eb.name) or is_in_hidden_folder(eb, EBOOK_DIR):
                    continue
                stem = eb.stem
                title_part = stem.split(" - ", 1)[0]
                metadata, series = read_local_ebook_metadata(eb), read_local_ebook_series(eb)
                metadata_title, metadata_authors = metadata
                if scan_metadata is not None:
                    stat = eb.stat()
                    scan_metadata[str(eb)] = {
                        "path": eb,
                        "metadata": metadata,
                        "series": series,
                        "mtime_ns": stat.st_mtime_ns,
                        "size": stat.st_size,
                    }
                for key in (_ebook_title_key(title_part), _ebook_title_key(stem), _ebook_title_key(metadata_title)):
                    if key:
                        index.setdefault(key, eb.name)
    except Exception as e:
        if scan_metadata is not None:
            scan_metadata["_error_type"] = type(e).__name__
        logger.warning(f"⚠️ Failed to build local ebook title index: {e}", exc_info=True)
    return index


def get_searchable_ebooks(
    search_term, inventory_candidates=None, source_states=None,
    full_refresh=False, local_metadata=None, abs_catalog=None,
):
    """Get ebook candidates, optionally collecting undeduped scan inventory."""
    discovery = source_states is not None
    regular_user = acting_user_is_regular(database_service)
    exclude_local_files = discovery and _scan_excludes_local_files()
    skip_local = exclude_local_files or regular_user
    inventory_candidates = inventory_candidates if inventory_candidates is not None else []
    scan_local_metadata = local_metadata if local_metadata is not None else ({} if discovery else None)
    results = []
    found_filenames = set()
    found_stems = set()  # To dedupe by title stem
    try:
        clients = _scan_user_clients()
    except Exception as exc:
        if discovery:
            _record_suggestions_source_state(
                source_states, "_client_registry", "ebook", True,
                complete=False, error_type=type(exc).__name__,
            )
            logger.warning("Could not load user-scoped ebook clients (%s)", type(exc).__name__, exc_info=True)
            return results
        raise

    local_index = None
    if discovery:
        if EBOOK_DIR.exists() and not regular_user:
            local_index = _build_local_ebook_title_index(scan_local_metadata)
            error_type = scan_local_metadata.get("_error_type") if isinstance(scan_local_metadata, dict) else None
            _record_suggestions_source_state(
                source_states, "Local File", "ebook", not exclude_local_files,
                complete=None if exclude_local_files else error_type is None,
                count=None if exclude_local_files or error_type else len(scan_local_metadata),
                error_type=None if exclude_local_files else error_type,
            )
        else:
            _record_suggestions_source_state(
                source_states, "Local File", "ebook", False, complete=None,
            )

    # 1. Grimmory
    booklore_client = getattr(clients, "booklore_client", None)
    booklore_configured = bool(booklore_client and booklore_client.is_configured())
    if discovery:
        _record_suggestions_source_state(
            source_states, "Grimmory", "ebook", booklore_configured,
            complete=None if not booklore_configured else False,
        )
    if booklore_configured:
        try:
            if search_term:
                books = booklore_client.search_books(search_term)
            else:
                # The ID view supplies every provider book to inverse discovery; the
                # default filename view below remains the legacy pair-candidate pool.
                if discovery:
                    all_id_books = booklore_client.get_all_books(
                        include_all_ids=True, force_refresh=full_refresh and "audio" not in source_states.get("Grimmory", {}).get("roles", {}),
                    )
                    _record_suggestions_source_state(
                        source_states, "Grimmory", "ebook", True,
                        complete=bool(getattr(booklore_client, "catalog_complete", False)),
                        count=len(all_id_books),
                        error_type=None if booklore_client.catalog_complete else "CatalogIncomplete",
                    )
                    for book in all_id_books or []:
                        primary = book.get('primaryFile') or {}
                        fname = book.get('fileName') or primary.get('fileName') or ''
                        file_type = str(book.get('bookType') or primary.get('bookType') or '').upper()
                        if not is_linkable_ebook_filename(fname) and file_type != 'EPUB':
                            continue
                        series_name, series_sequence = _series_from_library_detail(book)
                        inventory_candidates.append(EbookResult(
                            name=fname,
                            title=book.get('title'),
                            authors=book.get('authors'),
                            language=book.get('language'),
                            booklore_id=book.get('id'),
                            source='Grimmory',
                            path=book.get('filePath') or primary.get('filePath') or book.get('path'),
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=_first_not_none(book.get('sourceFileId'), primary.get('id')),
                            ebook_file_id=book.get('ebookFileId'),
                        ))
                    books = booklore_client.get_all_books()
                else:
                    books = booklore_client.get_all_books()
            if books:
                for b in books:
                    fname = b.get('fileName', '')
                    if is_linkable_ebook_filename(fname):
                        found_filenames.add(fname.lower())
                        found_stems.add(Path(fname).stem.lower())
                        series_name, series_sequence = _series_from_library_detail(b)
                        results.append(EbookResult(
                            name=fname,
                            title=b.get('title'),
                            subtitle=_ebook_edition_label(b),
                            authors=b.get('authors'),
                            language=b.get('language'),
                            booklore_id=b.get('id'),
                            path=b.get('filePath') or b.get('filepath') or b.get('path'),
                            source='Grimmory',
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=b.get('sourceFileId'),
                            ebook_file_id=b.get('ebookFileId'),
                        ))
        except Exception as e:
            if discovery:
                _record_suggestions_source_state(
                    source_states, "Grimmory", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                logger.warning("⚠️ Grimmory scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                logger.warning(f"⚠️ Grimmory search failed: {e}", exc_info=True)

    # 1b. BookOrbit
    bookorbit_client = getattr(clients, "bookorbit_client", None)
    bookorbit_configured = bool(bookorbit_client and bookorbit_client.is_configured())
    if discovery:
        _record_suggestions_source_state(
            source_states, "BookOrbit", "ebook", bookorbit_configured,
            complete=None if not bookorbit_configured else False,
        )
    if bookorbit_configured:
        try:
            if search_term:
                bo_books = bookorbit_client.search_ebooks(search_term)
                local_index = None
            else:
                if discovery and full_refresh and "audio" not in source_states.get("BookOrbit", {}).get("roles", {}):
                    bookorbit_client.get_all_books(force_refresh=True)
                bo_books = bookorbit_client.get_all_ebooks()
                if not discovery and not regular_user:
                    local_index = _build_local_ebook_title_index()
                if discovery:
                    complete = bool(getattr(bookorbit_client, "catalog_complete", False))
                    _record_suggestions_source_state(
                        source_states, "BookOrbit", "ebook", True,
                        complete=complete, count=len(bo_books or []),
                        error_type=None if complete else "CatalogIncomplete",
                    )
                    for book in bo_books or []:
                        ebook_format = str(book.get("ebookFormat") or "").lower().lstrip(".")
                        fname = book.get("fileName") or ""
                        if not is_linkable_ebook_filename(fname) and ebook_format not in {"epub", "cbz"}:
                            continue
                        series_name, series_sequence = _series_from_library_detail(book)
                        inventory_candidates.append(EbookResult(
                            name=fname,
                            title=book.get("title"),
                            authors=book.get("authors"),
                            language=book.get("language"),
                            source="BookOrbit",
                            source_id=book.get("id"),
                            path=book.get("filePath") or book.get("filepath") or book.get("path"),
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=book.get("sourceFileId"),
                            ebook_file_id=book.get("ebookFileId"),
                        ))
            for b in bo_books or []:
                fname = b.get('fileName') or ''
                if not fname and local_index is not None:
                    fname = local_index.get(_ebook_title_key(b.get('title'))) or ''
                if not fname and (discovery or local_index is not None):
                    # Index miss, or no index for a regular user: ask BookOrbit for this one
                    # file (#586). Interactive picker calls skip it; detail is cached 1h.
                    fname = (bookorbit_client._enrich_ebook(b.get('id'), b) or {}).get('fileName') or ''
                if not is_linkable_ebook_filename(fname):
                    continue
                if fname.lower() in found_filenames:
                    continue
                found_filenames.add(fname.lower())
                found_stems.add(Path(fname).stem.lower())
                series_name, series_sequence = _series_from_library_detail(b)
                results.append(EbookResult(
                    name=fname,
                    title=b.get('title'),
                    authors=b.get('authors'),
                    language=b.get('language'),
                    path=b.get('filePath') or b.get('filepath') or b.get('path'),
                    source='BookOrbit',
                    source_id=b.get('id'),
                    subtitle=_ebook_edition_label(b),
                    series_name=series_name,
                    series_sequence=series_sequence,
                    source_file_id=b.get('sourceFileId'),
                    ebook_file_id=b.get('ebookFileId'),
                ))
        except Exception as e:
            if discovery:
                _record_suggestions_source_state(
                    source_states, "BookOrbit", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                logger.warning("⚠️ BookOrbit scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                logger.warning(f"⚠️ BookOrbit search failed: {e}", exc_info=True)

    # BookFusion is searchable but does not expose a supported full catalog.
    bookfusion_client = getattr(clients, "bookfusion_client", None)
    if discovery:
        _record_suggestions_source_state(
            source_states, "BookFusion", "ebook",
            bool(bookfusion_client and bookfusion_client.is_configured()),
            supported=False, complete=None,
        )

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
    kavita_configured = bool(kavita_client and kavita_client.is_configured())
    if discovery:
        _record_suggestions_source_state(
            source_states, "Kavita", "ebook", kavita_configured,
            complete=None if not kavita_configured else False,
        )
    if kavita_configured:
        try:
            if search_term:
                kavita_books = kavita_client.search_ebooks(search_term)
            elif discovery:
                kavita_books = kavita_client.get_all_books(
                    force_refresh=full_refresh, require_complete=True,
                )
                complete = bool(getattr(kavita_client, "catalog_complete", False))
                _record_suggestions_source_state(
                    source_states, "Kavita", "ebook", True,
                    complete=complete, count=len(kavita_books or []),
                    error_type=None if complete else "CatalogIncomplete",
                )
            else:
                kavita_books = kavita_client.get_all_books()
            for book in kavita_books or []:
                filename = book.get('fileName') or book.get('filename') or ''
                if discovery and not search_term:
                    if is_linkable_ebook_filename(filename):
                        series_name, series_sequence = _series_from_library_detail(book)
                        inventory_candidates.append(EbookResult(
                            name=filename,
                            title=book.get('title'),
                            authors=book.get('authors') or book.get('author'),
                            language=book.get('language'),
                            source='Kavita',
                            source_id=book.get('id'),
                            path=book.get('filePath') or book.get('path'),
                            series_name=series_name,
                            series_sequence=series_sequence,
                            source_file_id=book.get('sourceFileId'),
                            ebook_file_id=book.get('ebookFileId'),
                        ))
                if not is_linkable_ebook_filename(filename):
                    continue
                if filename.lower() in found_filenames:
                    continue
                found_filenames.add(filename.lower())
                found_stems.add(Path(filename).stem.lower())
                series_name, series_sequence = _series_from_library_detail(book)
                results.append(EbookResult(
                    name=filename,
                    title=book.get('title'),
                    subtitle=_ebook_edition_label(book),
                    authors=book.get('authors') or book.get('author'),
                    language=book.get('language'),
                    path=book.get('filePath') or book.get('path'),
                    source='Kavita',
                    source_id=book.get('id'),
                    series_name=series_name,
                    series_sequence=series_sequence,
                    source_file_id=book.get('sourceFileId'),
                    ebook_file_id=book.get('ebookFileId'),
                ))
        except Exception as e:
            if discovery:
                _record_suggestions_source_state(
                    source_states, "Kavita", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                logger.warning("Kavita scan failed (%s)", type(e).__name__, exc_info=True)
            else:
                logger.warning("Kavita search failed: %s", e, exc_info=True)

    # 2. ABS ebook libraries
    abs_client = getattr(clients, "abs_client", None)
    abs_configured = bool(abs_client and abs_client.is_configured())
    if discovery:
        _record_suggestions_source_state(
            source_states, "ABS", "ebook", abs_configured,
            complete=None if not abs_configured else False,
        )
    if search_term:
        try:
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
            if discovery:
                _record_suggestions_source_state(
                    source_states, "ABS", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                logger.warning("⚠️ ABS ebook search failed (%s)", type(e).__name__, exc_info=True)
            else:
                logger.warning(f"⚠️ ABS ebook search failed: {e}", exc_info=True)

    if discovery and abs_configured:
        try:
            if abs_catalog is None:
                abs_catalog = abs_client.get_library_catalog(force_refresh=full_refresh)
            complete = isinstance(abs_catalog, list)
            _record_suggestions_source_state(
                source_states, "ABS", "ebook", True, complete=complete,
                count=len(abs_catalog) if complete else None,
                error_type=None if complete else "CatalogUnavailable",
            )
            for item in abs_catalog or []:
                media = item.get("media") or {}
                file_info = media.get("ebookFile")
                file_data = file_info if isinstance(file_info, dict) else {}
                file_text = file_info if isinstance(file_info, str) else ""
                ext = str(
                    file_data.get("ext") or media.get("ebookFormat")
                    or Path(file_text).suffix.lstrip(".")
                ).lower().lstrip(".")
                if not is_linkable_ebook_filename("file." + ext):
                    continue
                source_id = item.get("id")
                if source_id is None or str(source_id).strip() == "":
                    continue
                metadata = media.get("metadata") or item.get("metadata") or {}
                fname = file_data.get("fileName") or file_data.get("filename") or f"{source_id}_abs.{ext}"
                series_name, series_sequence = _series_from_abs_metadata(metadata)
                inventory_candidates.append(EbookResult(
                    name=fname,
                    title=metadata.get("title") or item.get("name"),
                    authors=metadata.get("authorName") or "",
                    language=metadata.get("language") or "",
                    source="ABS",
                    source_id=source_id,
                    series_name=series_name,
                    series_sequence=series_sequence,
                    source_file_id=(
                        _first_not_none(file_data.get("ino"), file_data.get("id"), file_data.get("fileId"))
                    ),
                    ebook_file_id=(
                        _first_not_none(file_data.get("ino"), file_data.get("id"), file_data.get("fileId"))
                    ),
                    path=file_data.get("path") or file_data.get("filePath"),
                ))
        except Exception as e:
            _record_suggestions_source_state(
                source_states, "ABS", "ebook", True,
                complete=False, error_type=type(e).__name__,
            )
            logger.warning("⚠️ ABS catalog capture failed (%s)", type(e).__name__, exc_info=True)

    # 3. CWA (Calibre-Web Automated)
    library_service = getattr(clients, "library_service", None)
    cwa_client = getattr(library_service, "cwa_client", None) if library_service else None
    cwa_configured = bool(cwa_client and cwa_client.is_configured())
    if discovery:
        _record_suggestions_source_state(
            source_states, "CWA", "ebook", cwa_configured,
            complete=None if not cwa_configured else False,
        )
    if search_term:
        try:
            if cwa_configured:
                cwa_results = cwa_client.search_ebooks(search_term)
                for cr in cwa_results or []:
                    fname = f"cwa_{cr.get('id', 'unknown')}.{cr.get('ext', 'epub')}"
                    if fname.lower() in found_filenames:
                        continue
                    cwa_id = cr.get('id')
                    abs_identifier = None
                    try:
                        calibre_resolver = container.calibre_identifier_resolver()
                    except Exception:
                        calibre_resolver = None
                    resolver_enabled = bool(calibre_resolver and calibre_resolver.is_enabled())
                    if resolver_enabled and cwa_id is not None:
                        try:
                            abs_identifier = calibre_resolver.get_abs_id(cwa_id)
                        except Exception as e:
                            logger.debug("Calibre identifier lookup failed for %s (%s)", cwa_id, type(e).__name__)
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
            if discovery:
                _record_suggestions_source_state(
                    source_states, "CWA", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                logger.warning("⚠️ CWA search failed (%s)", type(e).__name__, exc_info=True)
            else:
                logger.warning(f"⚠️ CWA search failed: {e}", exc_info=True)

    if discovery and cwa_configured:
        try:
            cwa_results = cwa_client.get_all_ebooks(force_refresh=full_refresh)
            complete = bool(getattr(cwa_client, "catalog_complete", False))
            _record_suggestions_source_state(
                source_states, "CWA", "ebook", True, complete=complete,
                count=len(cwa_results or []),
                error_type=None if complete else "CatalogIncomplete",
            )
            try:
                calibre_resolver = container.calibre_identifier_resolver()
            except Exception:
                calibre_resolver = None
            resolver_enabled = bool(calibre_resolver and calibre_resolver.is_enabled())
            abs_ids = None
            if resolver_enabled:
                try:
                    abs_ids = calibre_resolver.get_abs_ids([
                        record.get("id") for record in cwa_results or []
                        if record.get("id") is not None
                    ])
                    if abs_ids is None:
                        _record_suggestions_source_state(
                            source_states, "CWA", "ebook", True,
                            complete=False, error_type="CalibreIdentifierUnavailable",
                        )
                except Exception as e:
                    _record_suggestions_source_state(
                        source_states, "CWA", "ebook", True,
                        complete=False, error_type=type(e).__name__,
                    )
                    abs_ids = None
            for record in cwa_results or []:
                cwa_id = record.get("id")
                if cwa_id is None or str(cwa_id).strip() == "":
                    continue
                ext = str(record.get("ext") or "epub").lower().lstrip(".")
                fname = f"cwa_{cwa_id}.{ext}"
                if not is_linkable_ebook_filename(fname):
                    continue
                series_name, series_sequence = _series_from_library_detail(record)
                inventory_candidates.append(EbookResult(
                    name=fname,
                    title=record.get("title"),
                    authors=record.get("author") or "",
                    language=record.get("language") or "",
                    source="CWA",
                    source_id=cwa_id,
                    abs_identifier=(abs_ids or {}).get(str(cwa_id)) if resolver_enabled else None,
                    series_name=series_name,
                    series_sequence=series_sequence,
                ))
        except Exception as e:
            _record_suggestions_source_state(
                source_states, "CWA", "ebook", True,
                complete=False, error_type=type(e).__name__,
            )
            logger.warning("⚠️ CWA catalog capture failed (%s)", type(e).__name__, exc_info=True)

    # 4. Search filesystem (Local) - LOW PRIORITY
    if EBOOK_DIR.exists() and not skip_local:
        try:
            if discovery and scan_local_metadata is not None:
                local_files = [
                    details for key, details in scan_local_metadata.items()
                    if not str(key).startswith("_")
                ]
            else:
                local_files = [
                    {"path": eb, "metadata": None, "series": None,
                     "mtime_ns": eb.stat().st_mtime_ns, "size": eb.stat().st_size}
                    for eb in EBOOK_DIR.rglob('*')
                    if eb.suffix.lower() in LINKABLE_EBOOK_EXTENSIONS and eb.is_file()
                    and not is_generated_readalong_epub(eb.name)
                    and not is_in_hidden_folder(eb, EBOOK_DIR)
                ]
            for details in local_files:
                eb = details["path"]
                if eb.suffix.lower() not in LINKABLE_EBOOK_EXTENSIONS or not eb.is_file():
                    continue
                fname_lower = eb.name.lower()
                stem_lower = eb.stem.lower()
                metadata = details.get("metadata")
                series = details.get("series")
                if discovery:
                    meta_title, meta_authors = metadata or ("", "")
                    series_name, series_sequence = series or (None, None)
                    inventory_candidates.append(EbookResult(
                        name=eb.name,
                        title=meta_title or eb.stem,
                        authors=meta_authors,
                        path=eb,
                        source="Local File",
                        series_name=series_name,
                        series_sequence=series_sequence,
                        file_mtime_ns=details.get("mtime_ns"),
                        file_size=details.get("size"),
                    ))

                # Dedupe: if already found in rich source, skip
                if fname_lower in found_filenames or stem_lower in found_stems:
                    continue

                if not search_term or search_term.lower() in fname_lower:
                    result = EbookResult(name=eb.name, path=eb, source='Local File')
                    if discovery:
                        result._local_metadata = metadata or ("", "")
                        result._local_series = series or (None, None)
                    results.append(result)
                    found_filenames.add(fname_lower)
                    found_stems.add(stem_lower)

        except Exception as e:
            if discovery:
                _record_suggestions_source_state(
                    source_states, "Local File", "ebook", True,
                    complete=False, error_type=type(e).__name__,
                )
                logger.warning("⚠️ Filesystem scan failed (%s)", type(e).__name__, exc_info=True)
            else:
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


def _set_bookorbit_link_for_current_user(book, audio_source) -> None:
    """Point the acting user's BookOrbit link at ``book``.

    The per-user link outranks the book row in the BookOrbit resolvers, so a link
    created audio-only (e.g. by shelf-watch) hides the ebook until it is refreshed
    here, and a reader joining a shared book has no usable BookOrbit id without one.
    """
    user_id = get_current_user_id()
    if user_id is not None and "BookOrbit" in (audio_source, book.ebook_source):
        try:
            database_service.set_user_bookorbit_link(
                user_id,
                book.abs_id,
                ebook_id=book.ebook_source_id if book.ebook_source == "BookOrbit" else None,
                audio_id=book.audio_provider_book_id if audio_source == "BookOrbit" else None,
                title=book.abs_title,
            )
        except Exception as link_err:
            logger.warning(
                "Failed to set BookOrbit link for user %s on '%s': %s",
                user_id, sanitize_log_data(book.abs_id), link_err, exc_info=True,
            )


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
    access = _existing_mapping_access(
        existing_book,
        kosync_doc_id=kosync_doc_id,
        ebook_filename=resolved_ebook_filename,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
        ebook_source=ebook_source,
        ebook_source_id=ebook_source_id,
    )
    if access == "join":
        _set_bookorbit_link_for_current_user(existing_book, audio_source)
        return existing_book, None, None
    if access == "deny":
        return None, _FOREIGN_MAPPING_MESSAGE, 403
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

    if not _may_absorb_mapping(database_service.find_duplicate_mapping(target_book)):
        return None, _FOREIGN_MAPPING_MESSAGE, 403

    saved_book = database_service.save_book(target_book)

    # An ebook-only mapping for this same ebook may already exist -- adding an
    # audiobook to a book you already had as an ebook is exactly this path. Without
    # this, both rows survive and only one can own the KOSync document hash, so the
    # loser is served to devices but can never receive progress.
    database_service.absorb_duplicate_mapping(saved_book)

    _set_bookorbit_link_for_current_user(saved_book, audio_source)

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
                book=saved_book,
            )
        except Exception as shelf_err:
            logger.warning(f"Failed to shelve matched ebook '{shelf_filename}': {shelf_err}", exc_info=True)

    database_service.dismiss_suggestion(saved_book.abs_id)
    if isinstance(saved_book.kosync_doc_id, str) and saved_book.kosync_doc_id.strip():
        database_service.dismiss_suggestion(saved_book.kosync_doc_id)
    _adopt_kosync_progress_for_book(saved_book.abs_id, saved_book.kosync_doc_id)
    _enqueue_tracker_automatch(uc().sync_clients, saved_book)

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

    if existing_book is not None and not _acting_user_may_modify_book(existing_book.abs_id):
        # An audio-only re-match strips the text side, so it only leaves another
        # reader's mapping untouched when that mapping is audio-only already.
        if getattr(existing_book, "sync_mode", None) == "audiobook_only":
            logger.info(
                "🔗 '%s' is already mapped by another reader with the same text — adding it to this reader's library unchanged",
                sanitize_log_data(existing_book.abs_id),
            )
            return existing_book, None, None
        logger.info(
            "🔒 Refused to change mapping '%s': it belongs to another reader (%s would change)",
            sanitize_log_data(existing_book.abs_id), "text source",
        )
        return None, _FOREIGN_MAPPING_MESSAGE, 403

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

    access = _existing_mapping_access(
        existing_book,
        audio_source_id=str(audio_source_id),
        storyteller_uuid=storyteller_uuid,
        ebook_source="BookFusion",
        ebook_source_id=str(bookfusion_id),
    )
    if access == "join":
        _persist_bookfusion_link_for_current_user(
            existing_book.abs_id,
            "BookFusion",
            str(bookfusion_id),
            title=bookfusion_title or existing_book.abs_title,
            author=bookfusion_author,
        )
        return existing_book, None, None
    if access == "deny":
        return None, _FOREIGN_MAPPING_MESSAGE, 403
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

    if getattr(target_book, "kosync_doc_id", None) and not _may_absorb_mapping(
        database_service.find_duplicate_mapping(target_book)
    ):
        return None, _FOREIGN_MAPPING_MESSAGE, 403

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

from src.web.routes.system import api_health, api_restart, clean_inactive_cache, test_connection


from src.web.routes.settings import settings

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
    return normalize_series_key(name)


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

    # Single-card series design (issue #449): which child leads the collapsed
    # card. An in-progress child wins over an unstarted one -- readers care
    # about where they actually are, not just the next unread volume -- and
    # among several in-progress children the most recently synced one wins,
    # ties broken toward the earlier one in series order (children is already
    # sequence-sorted, and max() keeps the first maximal element).
    in_progress_indexed = [
        (i, c) for i, c in enumerate(children) if 0 < (c.get("unified_progress") or 0) < 100
    ]
    if in_progress_indexed:
        lead_index, lead_book = max(in_progress_indexed, key=lambda pair: pair[1].get("last_sync_unix") or 0.0)
    elif next_book is not None:
        lead_index, lead_book = next_index, next_book
    else:
        lead_index, lead_book = None, None

    if lead_book is not None:
        other_children = [c for i, c in enumerate(children) if i != lead_index]
        other_next_index = next(
            (i for i, c in enumerate(other_children) if (c.get("unified_progress") or 0) < 100),
            None,
        )
        other_preview_books, other_preview_hidden_count = _series_preview_books(
            other_children, other_next_index
        )
    else:
        other_children = []
        other_preview_books = []
        other_preview_hidden_count = 0

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
        "lead_book": lead_book,
        "other_children": other_children,
        "other_preview_books": other_preview_books,
        "other_preview_hidden_count": other_preview_hidden_count,
        "last_sync_unix": last_sync_unix,
        "added_at_unix": added_at_unix,
        "stack_cover_urls": [c.get("cover_url") for c in children[:3] if c.get("cover_url")],
        "section_bucket": "finished" if finished == total else "in_progress" if in_progress else "not_started",
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
            pending = database_service.get_pending_suggestion(key, user_id=get_current_user_id())
        except Exception as exc:
            logger.warning("Shelf-watch approval lookup failed for '%s': %s", key, exc, exc_info=True)
            continue
        if pending and getattr(pending, 'origin', None) == 'shelf_watch':
            metadata = pending.origin_metadata or {}
            if not isinstance(metadata, dict):
                return None
            if _is_other_book_in_watched_library(item, metadata):
                logger.info(
                    "Shelf-watch approval: queued ebook %s:%s is not the watched book %s; leaving '%s' on the shelf",
                    item.get('ebook_source'), item.get('ebook_source_id'),
                    metadata.get('grimmory_id'), sanitize_log_data(metadata.get('grimmory_filename') or ''),
                )
                return None
            return dict(metadata)
    return None


def _watch_library_key(source: str) -> str:
    """Grimmory's ebook cards say 'Grimmory'/'Booklore'; its watch metadata says 'BookLore'."""
    key = str(source or '').strip().lower()
    return 'booklore' if key == 'grimmory' else key


def _is_other_book_in_watched_library(item: dict, metadata: dict) -> bool:
    """True when the queued ebook is a different book from the watched book's own library.

    A copy from another library (a BookFusion edition, say) still completes the
    watch, but a second book from the same library is a different pairing.
    """
    watched_id = str(metadata.get('grimmory_id') or '').strip()
    queued_id = str(item.get('ebook_source_id') or '').strip()
    if not watched_id or not queued_id:
        return False
    same_library = (_watch_library_key(item.get('ebook_source'))
                    == _watch_library_key(metadata.get('source_name') or 'BookLore'))
    return same_library and queued_id != watched_id


def _watch_suggestion_review(source_id: str) -> "tuple[list, list, str] | None":
    """The recorded audiobook and ebook for Review on a shelf/reading-watch suggestion.

    Searching the suggested audiobook's title can miss it (blank metadata) or turn up
    another book's ebook, so Review offers exactly the pair the watcher scanned. The
    page pre-selects a lone result. Reads only the acting user's own suggestions.
    """
    pending = database_service.get_pending_suggestion(source_id, user_id=get_current_user_id())
    if not pending or getattr(pending, 'origin', None) not in ('shelf_watch', 'reading_watch'):
        return None
    metadata = pending.origin_metadata or {}
    match = next((m for m in pending.matches if m.get('bridge_key') == source_id), None)
    audiobooks = []
    if match:
        title = match.get('audio_title') or pending.title or ''
        audiobooks = [AudioResult(
            source=match.get('audio_source') or '',
            source_id=str(match.get('audio_source_id') or ''),
            title=title,
            authors=match.get('audio_author') or '',
            cover_url=match.get('audio_cover_url') or '',
            duration=match.get('audio_duration'),
            display_name=title,
            provider_book_id=match.get('audio_provider_book_id') or None,
            provider_file_id=match.get('audio_provider_file_id') or None,
        )]
    ebook_title = metadata.get('grimmory_title') or Path(metadata.get('grimmory_filename') or '').stem
    watched = (_watch_library_key(metadata.get('source_name') or 'BookLore'),
               str(metadata.get('grimmory_id') or ''))
    ebooks = [
        ebook for ebook in (_search_ebooks_with_fallback(ebook_title) if ebook_title else [])
        if (_watch_library_key(ebook.source), str(ebook.source_id or '')) == watched
    ]
    return audiobooks, ebooks, ebook_title.lower()


def _complete_shelf_watch_approval(meta: dict, *, remove_only: bool = False, book=None) -> bool:
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
        if remove_only or defer_shelf_add(database_service, book, get_current_user_id()):
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


_FORMAT_STRIP_EBOOK_CLIENTS = ("bookorbit", "booklore", "kavita", "cwa", "bookfusion", "absebook")
_FORMAT_STRIP_SERVICE_NAMES = {
    "abs": "Audiobookshelf", "absebook": "Audiobookshelf", "bookorbit": "BookOrbit",
    "bookorbitaudio": "BookOrbit", "booklore": "Grimmory", "bookloreaudio": "Grimmory",
    "kavita": "Kavita", "cwa": "Calibre-Web", "bookfusion": "BookFusion",
    "storyteller": "Storyteller", "kosync": "KOReader",
    "BookOrbit": "BookOrbit", "BookLore": "Grimmory", "Booklore": "Grimmory", "Kavita": "Kavita", "CWA": "Calibre-Web",
    "ABS": "Audiobookshelf", "BookFusion": "BookFusion",
}


def _format_strip_slot(leader_client: str | None) -> str | None:
    """Map a raw ReadingSession.leader_client to its library-card format slot.

    Unlike ``_dashboard_leader_service`` this keeps ABS ebook apart from ABS
    audio, because the two light different slots. Trackers return None."""
    if not leader_client:
        return None
    key = str(leader_client).strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "koreader"
    if key in ("abs", "bookorbitaudio", "bookloreaudio"):
        return "audio"
    if key in _FORMAT_STRIP_EBOOK_CLIENTS:
        return "ebook"
    if key == "storyteller":
        return "readalong"
    return None


def _build_format_strip(mapping: dict) -> list[dict]:
    """The library card's four format slots: ebook, audio, read-along, KOReader.

    Each slot says whether the book is linked there, the service and position
    standing for it, whether it led the last sync, and whether it is the
    position that disagrees on an out-of-sync book."""
    states = mapping.get("states") or {}
    ebook_states = [(c, states[c]) for c in _FORMAT_STRIP_EBOOK_CLIENTS if c in states]
    ebook_client, ebook_state = max(
        ebook_states, key=lambda item: item[1].get("last_updated") or 0, default=(None, None)
    )
    ebook_service = _FORMAT_STRIP_SERVICE_NAMES.get(ebook_client or mapping.get("ebook_source") or "")
    if not ebook_service and mapping.get("booklore_id"):
        ebook_service = "Grimmory"
    if not ebook_service and mapping.get("bookfusion_linked"):
        ebook_service = "BookFusion"
    audio_client = {"BookOrbit": "bookorbitaudio", "BookLore": "bookloreaudio"}.get(mapping.get("audio_source"), "abs")
    storyteller_linked = bool(
        mapping.get("storyteller_uuid") or mapping.get("storyteller_legacy_link") or "storyteller" in states
    )
    rows = [
        ("ebook", bool(ebook_client or ebook_service), ebook_service, ebook_state),
        ("audio", mapping.get("sync_mode") != "ebook_only", _FORMAT_STRIP_SERVICE_NAMES.get(audio_client), states.get(audio_client)),
        # service None = BookBridge's own generated read-along EPUB.
        ("readalong", storyteller_linked or bool(mapping.get("readalong_ready")),
         "Storyteller" if storyteller_linked else None, states.get("storyteller")),
        ("koreader", "kosync" in states, "KOReader", states.get("kosync")),
    ]
    last_slot = _format_strip_slot(mapping.get("last_sync_leader"))
    slots = [
        {
            "key": key,
            "linked": linked,
            "service": service,
            "pct": state.get("percentage") if linked and state else None,
            "last": linked and key == last_slot,
            "oos": False,
        }
        for key, linked, service, state in rows
    ]
    if mapping.get("is_out_of_sync"):
        # shortcut: compares raw percentages, so an audio slot (time axis) can be
        # picked on books with long front/back matter; use the text-axis values
        # from _compute_dashboard_sync_warning_pct if that shows up in practice.
        leader = next((s for s in slots if s["last"] and s["pct"]), None)
        reference = leader["pct"] if leader else mapping.get("unified_progress") or 0
        others = [s for s in slots if s["pct"] and s is not leader]
        if others:
            max(others, key=lambda s: abs(s["pct"] - reference))["oos"] = True
    return slots


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
    normalized_audio_source = _normalize_text_source_type(source)
    src_id = (audio_source_id or "").strip()
    aid = (abs_id or "").strip()

    if normalized_audio_source == "Booklore" and src_id:
        return f"/api/booklore/audiobook-cover/{src_id}"
    if normalized_audio_source == "BookOrbit" and src_id:
        return f"/api/bookorbit/audiobook-cover/{src_id}"
    if normalized_audio_source not in ("Booklore", "BookOrbit"):
        proxy_id = aid or src_id
        if proxy_id and not _is_synthetic_bridge_key(proxy_id):
            return f"/api/cover-proxy/{proxy_id}"

    ebook_src = (ebook_source or "").strip()
    ebook_id = (ebook_source_id or "").strip()
    if ebook_id:
        normalized_ebook_source = _normalize_text_source_type(ebook_src)
        if normalized_ebook_source == "Booklore":
            return f"/api/booklore/book-cover/{ebook_id}"
        if normalized_ebook_source == "BookOrbit":
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
        cached, stale = client.book_cache_snapshot()
        if cached:
            # Serve the last listing and refresh it off the request: an hourly
            # TTL expiry must not hold the page behind paginated provider calls.
            if stale:
                _spawn_user_background(client.get_all_books, label="bookorbit-cache-refresh")
            catalog = cached
        else:
            catalog = client.get_all_books() or []
        for info in catalog:
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
    goodreads_by_book=None,
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
        # A Spines-view height that stays with the book under any filter or sort (crc32: stable across processes).
        "spine_h": (196, 204, 214, 224)[zlib.crc32(book.abs_id.encode("utf-8")) % 4],
        "storyteller_uuid": book.storyteller_uuid,
        "added_at_unix": (claim_times_by_book or {}).get(book.abs_id, 0.0),
        "states": {},
    }

    if book.status in ("processing", "forging", "failed_retry_later", "failed_permanent", "error"):
        job = database_service.get_latest_job(book.abs_id, kind=JOB_KIND_ALIGNMENT)
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
            "storygraph_pages": storygraph_details.storygraph_pages,
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
            "storygraph_pages": None,
        })

    goodreads_details = (goodreads_by_book or {}).get(book.abs_id)
    mapping.update({
        "goodreads_book_id": goodreads_details.goodreads_book_id if goodreads_details else None,
        "goodreads_linked": bool(goodreads_details),
        "goodreads_url": goodreads_details.goodreads_url if goodreads_details else None,
        "goodreads_title": book.abs_title if goodreads_details else None,
        "goodreads_matched_by": goodreads_details.matched_by if goodreads_details else None,
        "goodreads_rating": _coerce_dashboard_rating(goodreads_details.goodreads_rating) if goodreads_details else None,
        "goodreads_review_count": _coerce_dashboard_count(goodreads_details.goodreads_review_count) if goodreads_details else None,
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

    cached_goodreads = _get_cached_goodreads_rating(book, cached_booklore_by_filename=cached_booklore_by_filename)
    for key in ("goodreads_rating", "goodreads_review_count"):
        if mapping[key] is None:
            mapping[key] = cached_goodreads.get(key)

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
    all_goodreads=None,
):
    hardcover_by_book = {h.abs_id: h for h in (all_hardcover or [])}
    storygraph_by_book = {s.abs_id: s for s in (all_storygraph or [])}
    goodreads_by_book = {g.abs_id: g for g in (all_goodreads or [])}
    states_by_book = _group_dashboard_states_by_book(all_states)
    reading_stats_by_book = reading_stats_by_book or {}
    cached_booklore_by_filename = cached_booklore_by_filename or {}
    claim_times_by_book = claim_times_by_book or {}
    if bookfusion_by_book is None:
        bookfusion_by_book = _prefetch_bookfusion_links(books, integrations)
    if bookorbit_authors is None:
        bookorbit_authors = _prefetch_bookorbit_authors(books, integrations)

    ctc_aligned_book_ids = database_service.get_ctc_aligned_book_ids()
    readalong_alignment_book_ids = database_service.get_readalong_alignment_book_ids()
    readalong_ready_book_ids = database_service.get_readalong_ready_book_ids()
    sync_markers = database_service.get_sync_markers()
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
            goodreads_by_book=goodreads_by_book,
        )
        mapping["ctc_aligned"] = book.abs_id in ctc_aligned_book_ids
        mapping["readalong_ready"] = book.abs_id in readalong_ready_book_ids
        mapping["last_sync_leader"], mapping["last_sync_leader_at"] = sync_markers.get(book.abs_id, (None, 0))
        mapping["last_sync_leader_when"] = _format_dashboard_last_sync(mapping["last_sync_leader_at"])
        mapping["format_strip"] = _build_format_strip(mapping)

        # Read-along generation eligibility (Phase 6a). Cheap, locally-computable
        # conditions only -- whether BookOrbit can actually resolve the audio
        # entry's real track files needs a live API call (resolve_audiobook_folder)
        # and is not affordable per book on every dashboard render, so it is left
        # to the action's own handler; this only surfaces what is knowable from
        # already-loaded rows, mirroring the ctc_aligned badge above.
        audio_ok = (
            getattr(book, "audio_source", None) == "BookOrbit"
            and getattr(book, "sync_mode", "audiobook") != "ebook_only"
        )
        mapping["readalong_audio_ok"] = audio_ok
        if not audio_ok:
            mapping["readalong_eligible"] = False
            mapping["readalong_ineligible_reason"] = (
                "This mapping has no audiobook to generate a read-along from."
                if getattr(book, "sync_mode", "audiobook") == "ebook_only"
                else "Read-along generation requires a BookOrbit audio source."
            )
        elif book.abs_id not in readalong_alignment_book_ids:
            mapping["readalong_eligible"] = False
            mapping["readalong_ineligible_reason"] = "This book has no CTC or lexical alignment map yet."
        else:
            mapping["readalong_eligible"] = True
            mapping["readalong_ineligible_reason"] = None

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




# ---------------- ROUTES ----------------
def _dashboard_mappings_for_user(user) -> tuple[list, float, dict]:
    """Mappings, overall progress and integration flags for one user's dashboard."""
    user_id = user.id if user else None
    books = database_service.get_all_books(user_id=user_id)
    all_states = database_service.get_all_states(
        user_id=user_id
    )
    books = _dashboard_visible_books_for_user(books, user)
    all_hardcover = database_service.get_all_hardcover_details()
    all_storygraph = database_service.get_all_storygraph_details()
    all_goodreads = database_service.get_all_goodreads_details()
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
        all_goodreads=all_goodreads,
        reading_stats_by_book=all_reading_stats,
        cached_booklore_by_filename=cached_booklore_by_filename,
        claim_times_by_book=claim_times_by_book,
    )
    return mappings, overall_progress, integrations


def _warm_dashboard_sync_warnings() -> None:
    """Build every user's dashboard mappings once at startup.

    The drift memo is empty after a restart and each audio-axis book then parses its
    whole alignment map, so the first dashboard load would pay for all of them.
    Running the real mapping build here fills the memo for the current positions.
    """
    registry = container.user_client_registry()
    for user in database_service.list_users():
        user_token = credentials_token = None
        try:
            bundle = registry.get_clients(user.id)
            user_token = set_current_user_id(user.id)
            credentials_token = set_current_user_credentials(getattr(bundle, 'credentials', None))
            mappings, _progress, _integrations = _dashboard_mappings_for_user(user)
            logger.info("Dashboard warm-up built %d mapping(s) for user %s", len(mappings), user.id)
        except Exception as exc:
            logger.warning("Dashboard warm-up failed for user %s: %s", user.id, exc, exc_info=True)
        finally:
            if credentials_token is not None:
                reset_current_user_credentials(credentials_token)
            if user_token is not None:
                reset_current_user_id(user_token)


def _pick_hero_mapping(mappings: list[dict]) -> dict | None:
    """Select the most recently propagated in-progress book, deterministically."""
    eligible = [
        mapping for mapping in mappings
        if 0 < float(mapping.get("unified_progress") or 0) < 100
        and any(float(state.get("percentage") or 0) > 0
                for state in (mapping.get("states") or {}).values())
    ]
    return max(
        eligible,
        key=lambda mapping: (
            float(mapping.get("last_sync_leader_at") or 0),
            float(mapping.get("last_sync_unix") or 0),
            str(mapping.get("abs_id") or ""),
        ),
        default=None,
    )


def _hero_leader_service(leader: str | None) -> str | None:
    """Display the exact service that led the latest propagated sync."""
    if not leader:
        return None
    key = str(leader).strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        return "KOReader"
    return _FORMAT_STRIP_SERVICE_NAMES.get(key)


def _hero_leader_percent(mapping: dict, leader: str | None) -> float | None:
    """Use the specific leader's position when it has an owned progress row."""
    key = str(leader or "").strip().lower()
    if key.startswith("kosync") or key == "bridgesync_plugin":
        key = "kosync"
    return (mapping.get("states") or {}).get(key, {}).get("percentage")


# Striped per-book locks prevent duplicate concurrent synopsis source reads.
_HERO_SYNOPSIS_LOCKS = tuple(threading.Lock() for _ in range(32))
# Sources answer None on a transient failure too, so an empty result is retried.
_HERO_SYNOPSIS_RECHECK_SECONDS = 7 * 86400


def _hero_plain_text(value: object) -> str:
    """Strip markup and scripts from untrusted source metadata."""
    if not isinstance(value, str):
        return ""
    safe = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", value, flags=re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", safe))).strip()[:12000]


def _hero_synopsis_source(book, user) -> str | None:
    """Use local EPUB, the reader's ABS metadata, then their BookOrbit detail."""
    for filename in dict.fromkeys((book.original_ebook_filename, book.ebook_filename)):
        if not filename or not str(filename).lower().endswith(".epub"):
            continue
        path = find_ebook_file(filename)
        if path:
            description = _hero_plain_text(extract_epub_metadata(str(path)).get("summary"))
            if description:
                return description
    bundle = container.user_client_registry().get_clients(user.id)
    abs_client = getattr(bundle, "abs_client", None)
    if abs_client and abs_client.is_configured():
        info = abs_client.get_item_details(book.abs_id)
        if isinstance(info, dict):
            media = info.get("media") if isinstance(info.get("media"), dict) else {}
            metadata = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
            description = _hero_plain_text(
                metadata.get("description") or media.get("description") or info.get("description")
            )
            if description:
                return description
    orbit_id = None
    if (book.ebook_source or "").lower() == "bookorbit":
        orbit_id = book.ebook_source_id
    elif (book.audio_source or "").lower() in ("bookorbit", "bookorbitaudio"):
        orbit_id = book.audio_source_id
    orbit = getattr(bundle, "bookorbit_client", None)
    if orbit_id and orbit and orbit.is_configured():
        detail = orbit.get_book_detail(orbit_id)
        if isinstance(detail, dict):
            nested = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
            description = _hero_plain_text(
                detail.get("description") or detail.get("summary")
                or nested.get("description") or nested.get("summary")
            )
            if description:
                return description
    return None


def _hero_cached_synopsis(book, user) -> str | None:
    """Keep a found synopsis per reader for good; retry an empty lookup after a week."""
    lock = _HERO_SYNOPSIS_LOCKS[hash(book.abs_id) % len(_HERO_SYNOPSIS_LOCKS)]
    with lock:
        checked_at, description = database_service.get_user_book_synopsis_cache(user.id, book.abs_id)
        if description or (checked_at and time.time() - checked_at < _HERO_SYNOPSIS_RECHECK_SECONDS):
            return description
        description = _hero_synopsis_source(book, user)
        database_service.set_user_book_synopsis_cache(user.id, book.abs_id, description, time.time())
        return description


def _hero_daily_totals(rows: list[dict], count: int, field: str, tz) -> dict:
    """Return today/week only with a complete history, never a truncated sample."""
    if not rows or count > len(rows):
        return {}
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    amounts = {"today": 0, "week": 0}
    for row in rows:
        try:
            when = row.get("date")
            date_value = (
                datetime.fromisoformat(when).date() if when
                else datetime.fromtimestamp(float(row["endTime"]), tz).date()
            )
            value = int(row.get(field) or 0)
        except (KeyError, TypeError, ValueError, OSError):
            return {}
        if monday <= date_value <= today:
            amounts["week"] += value
        if date_value == today:
            amounts["today"] += value
    return amounts


def _hero_listening_piece(book, abs_id: str) -> dict | None:
    """Per-reader duration totals, with optional local day/week breakdown."""
    abs_client = _stats_abs_client()
    detail = _abs_book_listening_detail(abs_client, abs_id) if abs_client else None
    if detail is None:
        detail = database_service.get_book_listening_detail(abs_id)
    if not detail or int(detail.get("totalSeconds") or 0) <= 0:
        return None
    payload = {"seconds": int(detail["totalSeconds"])}
    duration = float(book.duration or book.audio_duration or 0)
    if duration > 0:
        payload["lengthSeconds"] = round(duration)
        if detail.get("progressPercent") is not None:
            progress = min(100, max(0, float(detail["progressPercent"])))
            payload["leftSeconds"] = max(0, round(duration * (1 - progress / 100)))
    payload.update(_hero_daily_totals(
        detail.get("sessions") or [], int(detail.get("sessionCount") or 0),
        "durationSeconds", _get_stats_timezone(),
    ))
    return payload


def _hero_reading_piece(abs_id: str) -> dict | None:
    """Per-reader KOReader pages, enriched with catalog page totals where known."""
    tz = _get_stats_timezone()
    reading = database_service.get_koreader_book_detail(
        f"abs:{abs_id}", getattr(tz, "key", str(tz)),
    )
    if not reading or int(reading.get("pagesRead") or 0) <= 0:
        return None
    payload = {"read": int(reading["pagesRead"])}
    storygraph = database_service.get_storygraph_details(abs_id)
    hardcover = database_service.get_hardcover_details(abs_id)
    total = (
        getattr(storygraph, "storygraph_pages", None)
        or getattr(hardcover, "hardcover_pages", None)
    )
    if total and int(total) > 0:
        payload["total"] = int(total)
        payload["left"] = max(0, int(total) - payload["read"])
    heatmap = reading.get("heatmap") or []
    payload.update(_hero_daily_totals(heatmap, len(heatmap), "pages", tz))
    return payload


from src.web.routes.book import book_page, api_library_hero, api_book_synopsis




from src.web.routes.library import index


from src.web.routes.book_actions import api_current_audio_chapter, delete_mapping, delete_selected_mappings, clear_progress, remap_alignment, sync_now, mark_complete, update_hash




from src.web.routes.processing import job_queue_page, job_queue_reorder, job_queue_preflight_ack, shelfmark








from src.web.routes.forge import forge, forge_search_audio, forge_search_text, forge_process











from src.web.routes.alignments import alignments_llm_status, alignments_realign, alignments_restore






from src.web.routes.matching import match, batch_match, add_book, _legacy_book_linker_redirect

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


def _record_readalong_intent_for_match(abs_id: str) -> None:
    """Record "Also generate a read-along EPUB" for a non-forge match (Match All,
    suggestions auto-match).

    A book queued for alignment gets it consumed by the background job when its
    alignment completes (``SyncManager._dispatch_readalong_intent``). A re-match
    of a book whose alignment still applies stays 'active' and never re-runs that
    job, so it is dispatched here instead; the dispatcher consumes the intent
    exactly once and skips a book whose alignment is not eligible.
    """
    if not database_service.set_readalong_epub_requested(abs_id, True):
        return
    book = database_service.get_book(abs_id)
    if book is None or getattr(book, "status", None) != 'active':
        return
    try:
        container.forge_service().generate_readalong_if_requested(book)
    except Exception as e:
        logger.warning(
            "Read-along EPUB: could not dispatch generation for re-matched '%s': %s",
            abs_id, e, exc_info=True,
        )


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
                if audio_source == 'BookOrbit' and item.get('readalong_epub_requested'):
                    _record_readalong_intent_for_match(saved_book.abs_id)
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
        access = _existing_mapping_access(
            current_book_entry,
            ebook_filename=ebook_filename,
            audio_source_id=item['abs_id'],
            storyteller_uuid=storyteller_uuid or None,
            ebook_source=item.get('ebook_source'),
            ebook_source_id=item.get('ebook_source_id'),
        )
        if access == "deny":
            continue
        if access == "join":
            _claim_book_for_user_id(get_current_user_id(), item['abs_id'])
            continue

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
        if not _complete_shelf_watch_approval(shelf_watch_meta, book=book) and shelf_filename:
            _shelve_matched_ebook(shelf_filename, item.get('ebook_source'),
                                  item.get('ebook_source_id'), book=book)
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
            access = _existing_mapping_access(
                current_book_entry,
                ebook_filename=ebook_filename,
                audio_source_id=item['abs_id'],
                storyteller_uuid=storyteller_uuid or None,
                ebook_source=item.get('ebook_source'),
                ebook_source_id=item.get('ebook_source_id'),
            )
            if access == "deny":
                continue
            if access == "join":
                _claim_book_for_user_id(get_current_user_id(), item['abs_id'])
                continue

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
            if not _complete_shelf_watch_approval(shelf_watch_meta, book=book) and shelf_filename:
                _shelve_matched_ebook(
                    shelf_filename, item.get('ebook_source'), item.get('ebook_source_id'), book=book
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

            if not _may_replace_mapping(forge_id):
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
            # Phase 6b: read-along generation needs a finished alignment map, which
            # doesn't exist until this forge completes, so only the intent is
            # recorded here -- ForgeService._maybe_generate_readalong_epub consumes
            # it from the post-forge completion hook. BookOrbit-audio only (the
            # queue item is already gated to that; re-check here too since this
            # branch also covers BookLore).
            if audio_source == 'BookOrbit' and item.get('readalong_epub_requested'):
                database_service.set_readalong_epub_requested(forge_id, True)
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

            if not _may_replace_mapping(forge_id):
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

        _complete_shelf_watch_approval(shelf_watch_meta, book=book)
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
    if audio_source and ebook_filename and is_comic_ebook_filename(ebook_filename):
        # Comics cannot pair with audiobooks: the audio path would see empty ebook
        # text and could queue a pointless Whisper transcription. Queue the comic
        # as an ebook-only item instead.
        audio_source = None
        audio_source_id = None
        audio_provider_book_id = None
        audio_provider_file_id = None
        audio_duration = None
    storyteller_uuid = request.form.get('storyteller_uuid', '') or ''
    audio_only = (request.form.get('audio_only') or '').strip().lower() in {
        'true', '1', 'yes', 'on'
    }
    # Opt-in intent, recorded per queue item. Only meaningful for a
    # BookOrbit-audio item; honoured by both
    # Forge & Match and Match All (_record_readalong_intent_for_match). The UI
    # asks once, at Match All (_apply_queue_readalong_request); a direct POST
    # may still carry it, so re-check the value tolerantly ("on" and "true").
    readalong_epub_requested = (
        request.form.get('readalong_epub_requested') or ''
    ).strip().lower() in {'true', '1', 'yes', 'on'}

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
                Path(ebook_filename).stem if ebook_filename
                else _get_storyteller_display_metadata(storyteller_uuid).get('title') or 'Ebook'
            )
            audio_duration = None
            audio_cover_url = None
            audio_provider_book_id = None
            audio_provider_file_id = None

    if not bridge_key or not (ebook_filename or storyteller_uuid or audio_only):
        return None
    if ebook_filename and _refuse_regular_user_local_selection(
        ebook_source, ebook_source_path, ebook_source_id, ebook_filename,
    ):
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
        'ebook_source_path': _submitted_ebook_path(ebook_source_path),
        'storyteller_uuid': storyteller_uuid,
        'audio_only': audio_only and not (ebook_filename or storyteller_uuid),
        'readalong_epub_requested': readalong_epub_requested and audio_source == 'BookOrbit',
    }



def _suggestions_search_results(
    search: str, review: str, ebook_key: str, generation: str,
) -> tuple[list, list, list, bool, str, str]:
    """Run the Suggestions Search tab lookup.

    Returns (audiobooks, ebooks, storyteller_books, storyteller_search_failed,
    effective_search_term, mode) where mode is "review", "ebook_key" or "search".
    """
    clients = _scan_user_clients()
    storyteller_enabled = bool(clients.storyteller_client.is_configured())
    search = (search or '').strip().lower()
    review = (review or '').strip()
    ebook_key = (ebook_key or '').strip()
    audiobooks, ebooks, storyteller_books = [], [], []
    storyteller_search_failed = False
    mode = "search"
    review_pair = _watch_suggestion_review(review) if review else None
    if ebook_key:
        from src.services.suggestions_view import project_suggestions
        persisted = _load_persisted_suggestions_cache()
        snapshot = persisted.get("ebook_discovery") or {}
        if generation != snapshot.get("generation"):
            abort(409)
        projected = project_suggestions(
            tab="ebook_only", ebook_snapshot=snapshot,
            ebook_ignored=persisted.get("ebook_ignored"),
            mapped_books=database_service.get_all_books(user_id=get_current_user_id()),
        )
        entry = next((row for row in projected["entries"] if row["ebook_key"] == ebook_key), None)
        if entry is None:
            abort(409)
        selected = _suggestions_ebook_result(entry)
        if selected is None:
            abort(409)
        ebooks = [selected]
        search = search or entry.get("title") or ""
        audiobooks = _search_audiobooks_with_fallback(search) if search else []
        mode = "ebook_key"
    elif review_pair:
        audiobooks, ebooks, search = review_pair
        mode = "review"
    elif search:
        audiobooks = _search_audiobooks_with_fallback(search)
        ebooks = _search_ebooks_with_fallback(search)
        ebooks.sort(key=lambda x: x.name.lower())
        ebooks = _promote_authoritative_ebook_matches(audiobooks, ebooks)

    if search and storyteller_enabled:
        try:
            storyteller_books = clients.storyteller_client.search_books(search)
            if storyteller_books is None:
                storyteller_books = []
                storyteller_search_failed = True
        except Exception as e:
            logger.warning(f"⚠️ Storyteller search failed in suggestions search: {e}", exc_info=True)
            storyteller_search_failed = True
    return audiobooks, ebooks, storyteller_books, storyteller_search_failed, search, mode


def _legacy_search_redirect(code: int = 302):
    """Redirect a legacy Add Book/Match/Forge URL to the Suggestions Search tab."""
    args = request.args
    params = {
        'q': args.get('search') or args.get('q') or '',
        'review': args.get('review') or '',
        'ebook_key': args.get('ebook_key') or '',
        'generation': args.get('generation') or '',
    }
    params = {key: value for key, value in params.items() if value}
    return redirect(url_for('suggestions', tab='search', **params), code=code)






def _suggestions_ebook_result(entry: dict) -> Optional[EbookResult]:
    """Resolve one saved ebook identity for the existing Add Book workflow."""
    source = normalize_ebook_source(entry.get("source"))
    source_id = entry.get("source_id")
    filename = entry.get("filename") or ""
    path = entry.get("path")
    if source == "Local File":
        if _refuse_regular_user_local_selection(source, path, ebook_filename=filename):
            return None
        path = _safe_local_source_path(path)
        if not path or not Path(path).is_file():
            return None
        filename = path
    elif source == "BookOrbit":
        client = _scan_user_clients().bookorbit_client
        detail = client.get_book_detail(source_id, force=True, fresh_only=True)
        if not detail:
            return None
        primary = client._primary_file(detail, kind="ebook") or {}
        expected = _first_not_none(entry.get("source_file_id"), entry.get("ebook_file_id"))
        if expected is not None and str(primary.get("id")) != str(expected):
            return None
        filename = primary.get("filename") or ""
    elif source == "CWA":
        filename = f"cwa_{source_id}.epub"
    elif source == "ABS":
        filename = f"{source_id}_abs.epub"
    elif source not in {"Booklore", "Kavita"}:
        return None
    if not is_linkable_ebook_filename(filename):
        return None
    return EbookResult(
        name=filename, path=path, title=entry.get("title"),
        authors=entry.get("author"), language=entry.get("language"),
        source=source, source_id=source_id,
        series_name=entry.get("series_name"), series_sequence=entry.get("series_sequence"),
        source_file_id=entry.get("source_file_id"),
    )


def _resolve_suggestions_redesign_ebook_create(entry: dict) -> Optional[dict]:
    """Hash the selected provider document without a basename or provider fallback."""
    import tempfile

    selected = _suggestions_ebook_result(entry)
    if selected is None:
        return None
    source, source_id = selected.source, selected.source_id
    parser = container.ebook_parser()
    clients = _scan_user_clients() if source != "Local File" else None
    kosync_id = None
    if source == "Local File":
        kosync_id = parser.get_kosync_id(Path(selected.path))
    elif source in {"Booklore", "Kavita"}:
        client = clients.booklore_client if source == "Booklore" else clients.kavita_client
        content = client.download_book(source_id)
        if content:
            kosync_id = parser.get_kosync_id_from_bytes(selected.name, content)
    else:
        with tempfile.TemporaryDirectory(prefix="suggestions-ebook-") as directory:
            target = Path(directory) / ("selected" + Path(selected.name).suffix)
            if source == "BookOrbit":
                client = clients.bookorbit_client
                detail = client.get_book_detail(source_id, force=True, fresh_only=True)
                primary = client._primary_file(detail or {}, kind="ebook") or {}
                expected = _first_not_none(entry.get("source_file_id"), entry.get("ebook_file_id"))
                if expected is not None and str(primary.get("id")) != str(expected):
                    return None
                ok = primary.get("id") is not None and client.download_file_to_path(primary["id"], target)
            elif source == "CWA":
                client = clients.library_service.cwa_client
                detail = client.get_book_by_id(source_id)
                ok = (
                    detail and str(detail.get("id")) == str(source_id)
                    and detail.get("download_url")
                    and client.download_ebook(detail["download_url"], target)
                )
            else:
                client = clients.abs_client
                files = [file for file in client.get_ebook_files(source_id) if file.get("ext") == "epub"]
                expected = _first_not_none(entry.get("source_file_id"), entry.get("ebook_file_id"))
                if expected is not None:
                    files = [file for file in files if str(file.get("ino")) == str(expected)]
                if len(files) != 1:
                    return None
                ok = client.download_file(files[0]["stream_url"], target)
            if ok and target.is_file():
                kosync_id = parser.get_kosync_id(target)
    if not isinstance(kosync_id, str) or not kosync_id.strip():
        return None
    return {
        "ebook_filename": selected.name, "ebook_source": source,
        "ebook_source_id": source_id, "ebook_source_path": str(selected.path or ""),
        "precomputed_kosync_doc_id": kosync_id,
    }


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


def _get_ignored_suggestion_source_ids(user_id: Optional[int] = None):
    """Return suggestion source IDs (bridge keys) the user marked as ignored."""
    return _get_suggestions_service().get_ignored_suggestion_source_ids(user_id=user_id)


def _suggestions_discovery_sources_complete(source_states):
    return all(
        role.get("complete") is True
        for state in source_states.values()
        for role in (state.get("roles") or {}).values()
        if role.get("configured") and role.get("supported", True)
    )


def _invalidate_suggestions_pair_cache(cached_suggestions, cached_no_match, previous, current):
    previous = previous if isinstance(previous, dict) else {}
    old_audio = previous.get("audio_fingerprints_by_key") or {}
    new_audio = current.get("audio_fingerprints_by_key") or {}
    invalid = {
        key for key in set(old_audio) | set(new_audio)
        if old_audio.get(key) != new_audio.get(key)
    }
    old_entries = previous.get("entries") or {}
    new_entries = current.get("entries") or {}
    for key in set(old_entries) | set(new_entries):
        old = old_entries.get(key) or {}
        new = new_entries.get(key) or {}
        if old.get("fingerprint") != new.get("fingerprint"):
            invalid.update(old.get("audio_keys") or [])
            invalid.update(new.get("audio_keys") or [])
            invalid.update(old.get("candidate_audio_keys") or [])
            invalid.update(new.get("candidate_audio_keys") or [])
            if new.get("unknown_reason") == "candidate_search_incomplete":
                invalid.update(cached_no_match or [])
    if not invalid:
        return cached_suggestions, cached_no_match, 0
    filtered = {
        key: value for key, value in (cached_suggestions or {}).items()
        if key not in invalid
    }
    no_match = [key for key in cached_no_match or [] if key not in invalid]
    return filtered, no_match, len(invalid)


def _reconcile_discovery_with_pair_matches(snapshot, cache_by_abs):
    from src.services.ebook_discovery import ebook_identity_keys

    identity_to_audio = {}
    for bridge_key, suggestion in (cache_by_abs or {}).items():
        for match in suggestion.get("matches") or []:
            match_record = dict(match)
            match_record["path"] = match.get("source_path")
            for identity in ebook_identity_keys(match_record):
                identity_to_audio.setdefault(identity, set()).add(str(bridge_key))
    for entry in (snapshot.get("entries") or {}).values():
        if entry.get("classification") != "no_audio":
            continue
        bridge_keys = set()
        for identity in ebook_identity_keys(entry):
            bridge_keys.update(identity_to_audio.get(identity, ()))
        if bridge_keys:
            entry["classification"] = "has_audio"
            entry["audio_keys"] = sorted(set(entry.get("audio_keys") or []) | bridge_keys)
            entry["search_complete"] = True
    stats = snapshot.get("stats") or {}
    stats["no_audio"] = sum(
        1 for entry in (snapshot.get("entries") or {}).values()
        if entry.get("classification") == "no_audio"
    )
    stats["has_audio"] = sum(
        1 for entry in (snapshot.get("entries") or {}).values()
        if entry.get("classification") == "has_audio"
    )


def scan_library_suggestions(
    cached_suggestions_by_abs=None, cached_no_match_abs_ids=None,
    progress_callback=None, full_refresh=False,
):
    """Run one provider scan, retaining inverse discovery and legacy pair results."""
    from copy import deepcopy
    from src.services.ebook_discovery import build_discovery_snapshot, build_ebook_inventory

    attempted_at = time.time()
    persisted = _load_persisted_suggestions_cache()
    previous = persisted.get("ebook_discovery") or {}
    exclude_local_files = _scan_excludes_local_files()
    exclude_setting_changed = bool(previous.get("exclude_local_files", False)) != exclude_local_files
    if exclude_setting_changed:
        cached_suggestions_by_abs = {}
        cached_no_match_abs_ids = []
        previous = {}
    ignored = persisted.get("ebook_ignored") or {}
    source_states = {}
    inventory_candidates = []
    local_metadata = {}
    service = _get_suggestions_service()
    try:
        clients = _scan_user_clients()
        abs_client = getattr(clients, "abs_client", None)
        abs_catalog = None
        if abs_client and abs_client.is_configured():
            try:
                abs_catalog = abs_client.get_library_catalog(force_refresh=full_refresh)
                _record_suggestions_source_state(
                    source_states, "ABS", "audio", True,
                    complete=isinstance(abs_catalog, list),
                    count=len(abs_catalog) if isinstance(abs_catalog, list) else None,
                    error_type=None if isinstance(abs_catalog, list) else "CatalogUnavailable",
                )
            except Exception as exc:
                _record_suggestions_source_state(
                    source_states, "ABS", "audio", True,
                    complete=False, error_type=type(exc).__name__,
                )
        audio_candidates = get_suggestion_audiobooks(
            source_states=source_states, abs_catalog=abs_catalog, full_refresh=full_refresh,
        )
        ebook_candidates = get_searchable_ebooks(
            "", inventory_candidates=inventory_candidates, source_states=source_states,
            full_refresh=full_refresh, local_metadata=local_metadata, abs_catalog=abs_catalog,
        )
    except Exception as exc:
        _record_suggestions_source_state(
            source_states, "_catalog_capture", "ebook", True,
            complete=False, error_type=type(exc).__name__,
        )
        audio_candidates = []
        ebook_candidates = []
        logger.warning("Suggestions catalog capture failed (%s)", type(exc).__name__, exc_info=True)

    if not _suggestions_discovery_sources_complete(source_states):
        snapshot = dict(previous) if isinstance(previous, dict) else {}
        snapshot.setdefault("schema_version", 1)
        snapshot.setdefault("generation", None)
        snapshot.setdefault("completed_at", None)
        snapshot.setdefault("entries", {})
        snapshot["state"] = "incomplete"
        snapshot["exclude_local_files"] = exclude_local_files
        snapshot["error"] = "source_incomplete"
        snapshot["attempted_at"] = attempted_at
        snapshot["source_states"] = source_states
        cached = {} if exclude_setting_changed else persisted.get("scan_cache_by_abs", {}) or {}
        no_match = [] if exclude_setting_changed else persisted.get("scan_cache_no_match_abs_ids", []) or []
        return {
            "suggestions": list(cached.values()),
            "cache_by_abs": cached,
            "no_match_abs_ids": no_match,
            "stats": persisted.get("scan_last_stats", {}) or {},
            "ebook_discovery": snapshot,
            "source_states": source_states,
            "discovery_complete": False,
            "_exclude_setting_changed": exclude_setting_changed,
            "scan_generation": persisted.get("scan_generation"),
            "scanned_at": persisted.get("scanned_at", 0.0),
        }

    mapped_books = list(service.database_service.get_all_books(user_id=get_current_user_id()) or [])
    service.get_audiobooks_conditionally = lambda: audio_candidates
    original_ebook_loader = service.get_searchable_ebooks
    service.get_searchable_ebooks = lambda term: ebook_candidates if not term else original_ebook_loader(term)

    inventory = build_ebook_inventory(
        inventory_candidates, previous_entries=(previous.get("entries") or {}),
        exclude_local_files=exclude_local_files,
    )
    audio_pool = service._build_audiobook_candidate_pool(audio_candidates)
    # Some Grimmory summary rows omit companion formats. A plausible match to
    # such a row is unknown, never evidence that the ebook has no audio.
    booklore = getattr(clients, "booklore_client", None)
    if booklore and source_states.get("Grimmory", {}).get("roles", {}).get("audio", {}).get("configured"):
        uncertain = []
        for book in booklore.get_all_books(include_all_ids=True):
            if (
                book.get("_needs_detail") and not booklore._book_supports_audiobook(book)
                and not booklore._has_audio_shape_fields(book)
            ):
                uncertain.append({
                    "audio_source": "BookLore", "audio_source_id": book.get("id"),
                    "audio_title": book.get("title") or book.get("fileName") or "",
                    "audio_author": book.get("authors") or "",
                    "audio_path": book.get("filePath") or "",
                })
        for candidate in service._build_audiobook_candidate_pool(uncertain):
            candidate["audio_inventory_unknown"] = True
            audio_pool.append(candidate)
    snapshot = build_discovery_snapshot(
        service, inventory, audio_pool, previous=previous,
        mapped_books=mapped_books, ignored=ignored, force_refresh=full_refresh,
        now=attempted_at,
    )
    snapshot["source_states"] = source_states
    cached_suggestions = deepcopy(cached_suggestions_by_abs or {})
    cached_no_match = list(cached_no_match_abs_ids or [])
    cached_suggestions, cached_no_match, invalidated = _invalidate_suggestions_pair_cache(
        cached_suggestions, cached_no_match, previous, snapshot,
    )
    logger.info(
        "Suggestions discovery inventory=%d audio=%d invalidated_pair_anchors=%d",
        len(inventory), len(audio_candidates), invalidated,
    )
    results = service.scan_library_suggestions(
        cached_suggestions_by_abs=cached_suggestions,
        cached_no_match_abs_ids=cached_no_match,
        progress_callback=progress_callback,
    )
    try:
        clients = uc()
        for source, client in (
            ("Grimmory", getattr(clients, "booklore_client", None)),
            ("BookOrbit", getattr(clients, "bookorbit_client", None)),
            ("Kavita", getattr(clients, "kavita_client", None)),
        ):
            roles = source_states.get(source, {}).get("roles", {})
            if client and any(
                state.get("configured") and state.get("supported", True)
                and state.get("complete") is True for state in roles.values()
            ) and not bool(getattr(client, "catalog_complete", False)):
                for role, state in roles.items():
                    if state.get("configured") and state.get("supported", True):
                        state["complete"] = False
                        state["error_type"] = "CatalogIncompleteAfterPairScan"
        library_service = getattr(clients, "library_service", None)
        cwa = getattr(library_service, "cwa_client", None) if library_service else None
        cwa_roles = source_states.get("CWA", {}).get("roles", {})
        if cwa and any(
            state.get("configured") and state.get("complete") is True
            for state in cwa_roles.values()
        ) and not bool(getattr(cwa, "catalog_complete", False)):
            for state in cwa_roles.values():
                if state.get("configured") and state.get("supported", True):
                    state["complete"] = False
                    state["error_type"] = "CatalogIncompleteAfterPairScan"
    except Exception as exc:
        _record_suggestions_source_state(
            source_states, "_post_scan_health", "ebook", True,
            complete=False, error_type=type(exc).__name__,
        )
    if not _suggestions_discovery_sources_complete(source_states):
        stale_snapshot = dict(previous) if isinstance(previous, dict) else {}
        stale_snapshot.setdefault("schema_version", 1)
        stale_snapshot.setdefault("generation", None)
        stale_snapshot.setdefault("completed_at", None)
        stale_snapshot.setdefault("entries", {})
        stale_snapshot["state"] = "incomplete"
        stale_snapshot["exclude_local_files"] = exclude_local_files
        stale_snapshot["error"] = "source_incomplete"
        snapshot["error"] = "source_incomplete"
        stale_snapshot["attempted_at"] = attempted_at
        stale_snapshot["source_states"] = source_states
        cached = {} if exclude_setting_changed else persisted.get("scan_cache_by_abs", {}) or {}
        no_match = [] if exclude_setting_changed else persisted.get("scan_cache_no_match_abs_ids", []) or []
        return {
            "suggestions": list(cached.values()),
            "cache_by_abs": cached,
            "no_match_abs_ids": no_match,
            "stats": persisted.get("scan_last_stats", {}) or {},
            "ebook_discovery": stale_snapshot,
            "source_states": source_states,
            "discovery_complete": False,
            "_exclude_setting_changed": exclude_setting_changed,
            "scan_generation": persisted.get("scan_generation"),
            "scanned_at": persisted.get("scanned_at", 0.0),
        }
    _reconcile_discovery_with_pair_matches(snapshot, results.get("cache_by_abs", {}))
    results["scan_generation"] = uuid.uuid4().hex
    results["ebook_discovery"] = snapshot
    results["source_states"] = source_states
    results["discovery_complete"] = True
    results["scanned_at"] = attempted_at
    return results


def _prune_suggestions_scan_jobs():
    cutoff = time.time() - SUGGESTIONS_SCAN_JOB_TTL_SECONDS
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        stale_ids = [
            job_id for job_id, job in SUGGESTIONS_SCAN_JOBS.items()
            if job.get('status') != 'queued'
            and job.get('updated_at', job.get('started_at', 0)) < cutoff
        ]
        for job_id in stale_ids:
            SUGGESTIONS_SCAN_JOBS.pop(job_id, None)


SUGGESTIONS_AUTO_SCAN_STATE_KEY = "SUGGESTIONS_AUTO_SCAN_STATE"
SUGGESTIONS_AUTO_SCAN_MIN_MINUTES = 5
_SUGGESTIONS_AUTO_SCAN_JOB = {}


_CRON_FIELDS = (
    (0, 59, {}),
    (0, 23, {}),
    (1, 31, {}),
    (1, 12, {name: i for i, name in enumerate(
        ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), start=1)}),
    (0, 7, {name: i for i, name in enumerate(('sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'))}),
)
_SUGGESTIONS_WEEKDAYS = ('sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday')


def parse_cron_expression(expression: str):
    """Parse a five-field cron expression into the set of values each field allows.

    Supports `*`, numbers, `a-b` ranges, `/step`, comma lists and three-letter month and
    weekday names. Day of week accepts 0-7, where 0 and 7 are both Sunday. Raises
    ValueError for anything else.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 fields, got {len(fields)}")
    allowed = []
    for text, (low, high, names) in zip(fields, _CRON_FIELDS):
        values = set()
        for part in text.lower().split(','):
            span, _, step = part.partition('/')
            if span == '*':
                start, end = low, high
            else:
                first, _, last = span.partition('-')
                start = names[first] if first in names else int(first)
                end = (names[last] if last in names else int(last)) if last else (high if step else start)
            stride = int(step) if step else 1
            if not low <= start <= end <= high or stride < 1:
                raise ValueError(f"'{part}' is outside {low}-{high}")
            values.update(range(start, end + 1, stride))
        allowed.append(values)
    if 7 in allowed[4]:
        allowed[4].add(0)
    # As in cronie, a field starting with '*' (including '*/n') leaves its day unrestricted.
    return allowed, not fields[2].startswith('*'), not fields[4].startswith('*')


def cron_matches(cron, when) -> bool:
    """Whether a parsed cron expression fires at `when`, to the minute.

    As in standard cron, when both day of month and day of week are restricted, a day
    matching either one fires.
    """
    (minutes, hours, days, months, weekdays), day_restricted, weekday_restricted = cron
    if when.minute not in minutes or when.hour not in hours or when.month not in months:
        return False
    day_ok = when.day in days
    weekday_ok = when.isoweekday() % 7 in weekdays
    if day_restricted and weekday_restricted:
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def _suggestions_full_refresh_schedule():
    if env_truthy('SUGGESTIONS_FULL_REFRESH_ENABLED'):
        expression = (os.environ.get('SUGGESTIONS_FULL_REFRESH_CRON') or '').strip()
        return ('SUGGESTIONS_FULL_REFRESH_CRON', expression) if expression else (None, None)
    day = (os.environ.get('SUGGESTIONS_FULL_REFRESH_DAY') or 'off').strip().lower()
    if day not in _SUGGESTIONS_WEEKDAYS:
        return None, None
    raw_time = (os.environ.get('SUGGESTIONS_FULL_REFRESH_TIME') or '04:00').strip()
    try:
        refresh_time = datetime.strptime(raw_time, '%H:%M').time()
    except ValueError:
        return 'SUGGESTIONS_FULL_REFRESH_TIME', raw_time
    return 'SUGGESTIONS_FULL_REFRESH_DAY', f"{refresh_time.minute} {refresh_time.hour} * * {day[:3]}"


def _suggestions_full_refresh_due(now, state) -> Optional[str]:
    """The cron fire a full refresh is owed for, as an ISO string, or None.

    The latest fire within the past day stays owed until a refresh has run for it, so a
    fire missed while BookBridge was down still runs once it is back.
    """
    source, expression = _suggestions_full_refresh_schedule()
    if not expression:
        return None
    try:
        cron = parse_cron_expression(expression)
    except ValueError:
        get_persistent_condition_logger().warn(
            logger,
            "suggestions-full-refresh-schedule",
            "⚠️ %s '%s' is not a valid schedule — full refresh is off",
            source,
            sanitize_log_data(expression),
            exc_info=True,
        )
        return None
    get_persistent_condition_logger().resolve(
        logger, "suggestions-full-refresh-schedule", "Full refresh schedule is valid again",
    )
    # Step back through real instants (UTC) so the repeated hour of a fall-back change is
    # walked in order instead of being skipped by wall-clock arithmetic.
    fire = now.replace(second=0, microsecond=0)
    instant = fire.astimezone(timezone.utc) if fire.tzinfo else None
    for _ in range(24 * 60 + 1):
        if cron_matches(cron, fire):
            return None if _suggestions_full_refresh_done(fire, state) else fire.isoformat()
        if instant is None:
            fire -= timedelta(minutes=1)
        else:
            instant -= timedelta(minutes=1)
            fire = instant.astimezone(fire.tzinfo)
    return None


def _suggestions_full_refresh_done(fire: datetime, state: dict) -> bool:
    """Whether a full refresh already covers the cron fire at `fire`.

    A recorded fire covers every fire at or before it, and also the same wall-clock time
    repeated by a fall-back change, so one schedule slot never runs twice.
    """
    if state.get("last_full_date") == fire.date().isoformat():
        return True
    try:
        last = datetime.fromisoformat(state.get("last_full_fire") or "")
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None or fire.tzinfo is None:
        return fire.replace(tzinfo=None) <= last.replace(tzinfo=None)
    return fire <= last or fire.replace(tzinfo=None) == last.replace(tzinfo=None)


def _suggestions_auto_scan_due(now, state):
    if _suggestions_full_refresh_due(now, state):
        return 'full'

    try:
        minutes = int(float(os.environ.get('SUGGESTIONS_AUTO_SCAN_MINUTES') or 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return None
    interval_seconds = max(minutes, SUGGESTIONS_AUTO_SCAN_MIN_MINUTES) * 60
    if now.timestamp() - float(state.get("last_finished") or 0) >= interval_seconds:
        return 'incremental'
    return None


def _run_scheduled_suggestions_scan(full: bool):
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

    user_id = database_service._default_user_id()
    if user_id is None:
        return None
    creds = dict(database_service.get_user_credentials(user_id) or {})
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = _global_fallback_allowed(
        database_service, database_service.get_user(user_id),
    )

    tok_bundle = _active_bundle.set(container.user_client_registry().get_clients(user_id))
    tok_uid = set_current_user_id(user_id)
    tok_creds = set_current_user_credentials(creds)
    try:
        if full:
            cached_by_abs, cached_no_match = {}, []
        else:
            persisted = _load_persisted_suggestions_cache()
            cached_by_abs = persisted.get('scan_cache_by_abs', {}) or {}
            cached_no_match = persisted.get('scan_cache_no_match_abs_ids', []) or []
        return _start_suggestions_scan_job(
            cached_suggestions_by_abs=cached_by_abs,
            cached_no_match_abs_ids=cached_no_match,
            full_refresh=full,
        )
    finally:
        reset_current_user_credentials(tok_creds)
        reset_current_user_id(tok_uid)
        _active_bundle.reset(tok_bundle)


def _suggestions_auto_scan_tick():
    if not env_truthy('SUGGESTIONS_ENABLED'):
        return
    try:
        state = database_service.get_json_setting(SUGGESTIONS_AUTO_SCAN_STATE_KEY, default={})
        if not isinstance(state, dict):
            state = {}

        if _SUGGESTIONS_AUTO_SCAN_JOB:
            job = _get_suggestions_scan_job(_SUGGESTIONS_AUTO_SCAN_JOB["job_id"])
            if job and job["status"] in _SCAN_JOB_ACTIVE:
                return
            state["last_finished"] = job["updated_at"] if job else time.time()
            if _SUGGESTIONS_AUTO_SCAN_JOB["full_fire"]:
                state["last_full_fire"] = _SUGGESTIONS_AUTO_SCAN_JOB["full_fire"]
                state.pop("last_full_date", None)
            database_service.set_json_setting(SUGGESTIONS_AUTO_SCAN_STATE_KEY, state)
            with SUGGESTIONS_SCAN_JOBS_LOCK:
                SUGGESTIONS_SCAN_JOBS.pop(_SUGGESTIONS_AUTO_SCAN_JOB["job_id"], None)
            _SUGGESTIONS_AUTO_SCAN_JOB.clear()

        now = datetime.now(_get_stats_timezone())
        due = _suggestions_auto_scan_due(now, state)
        with SUGGESTIONS_SCAN_JOBS_LOCK:
            scan_running = any(job.get("status") in _SCAN_JOB_ACTIVE for job in SUGGESTIONS_SCAN_JOBS.values())
        if due and not scan_running:
            job_id = _run_scheduled_suggestions_scan(full=(due == 'full'))
            if job_id:
                _SUGGESTIONS_AUTO_SCAN_JOB.update(
                    job_id=job_id,
                    full_fire=_suggestions_full_refresh_due(now, state) if due == 'full' else None,
                )
                logger.info("🔎 Scheduled suggestions scan started (%s)", due)
        get_persistent_condition_logger().resolve(
            logger, "suggestions-auto-scan", "Scheduled suggestions scans resumed",
        )
    except Exception as e:
        get_persistent_condition_logger().warn(
            logger, "suggestions-auto-scan", "Scheduled suggestions scan tick failed: %s", e, exc_info=True,
        )


def _start_suggestions_scan_job(cached_suggestions_by_abs=None, cached_no_match_abs_ids=None, full_refresh=False):
    _prune_suggestions_scan_jobs()
    bundle = _scan_user_clients()
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

    job_id = uuid.uuid4().hex
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        # Reuse this reader's queued or running scan; checked under the same lock
        # as the insert so two clicks can never start two scans.
        if user_id:
            for existing_id, existing in SUGGESTIONS_SCAN_JOBS.items():
                if existing.get('user_id') == user_id and existing.get('status') in _SCAN_JOB_ACTIVE:
                    logger.info(f"Suggestions scan already running; reusing job {existing_id}")
                    return existing_id
        SUGGESTIONS_SCAN_JOBS[job_id] = {
            "status": "queued",
            "user_id": user_id,
            "results": {},
            "error": None,
            "progress": {
                "phase": "queued",
                "percent": 0,
                "message": "Waiting for another library scan to finish...",
                "scanned_new_done": 0,
                "scanned_new_total": 0,
                "reused_cached": 0,
                "total_unmatched": 0,
            },
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    def runner():
        tok_bundle = _active_bundle.set(bundle)
        tok_uid = set_current_user_id(user_id)
        tok_creds = set_current_user_credentials(creds)
        try:
            with SUGGESTIONS_SCAN_SLOT:
                with SUGGESTIONS_SCAN_JOBS_LOCK:
                    job = SUGGESTIONS_SCAN_JOBS.get(job_id)
                    if job is None:
                        return
                    job["status"] = "running"
                    job["progress"] = {**job["progress"], "phase": "initializing", "message": "Preparing scan..."}
                    job["updated_at"] = time.time()
                _run_suggestions_scan_job(
                    job_id,
                    cached_suggestions_by_abs or {},
                    cached_no_match_abs_ids or [],
                    full_refresh,
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
    With SUGGESTIONS_AUTO_MATCH_READALONG on, a BookOrbit-audio match also
    requests a read-along EPUB.

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

    from src.services.suggestions_service import SuggestionsService

    threshold = _auto_match_threshold()
    mapping_service = container.book_mapping_service()
    cache_by_abs = results.get('cache_by_abs') or {}
    remaining = []
    matched = 0

    for suggestion in suggestions:
        matches = suggestion.get('matches') or []
        eligible_matches = [m for m in matches if not _is_same_folder_match(m)
                            and not is_comic_ebook_filename(m.get('ebook_filename'))]
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
        block = SuggestionsService.bulk_ineligibility(
            {'matches': sorted(eligible_matches, key=lambda m: m.get('score') or 0.0, reverse=True)},
            min_score=threshold, allow_local_file=True,
        )
        if block:
            logger.info(
                "Auto-match left '%s' for review (score=%.1f): %s",
                suggestion.get('abs_title'), score, block,
            )
            remaining.append(suggestion)
            continue
        try:
            # The mapping service hashes by downloading from a library; a Local File
            # has no library id, so hash the file itself as manual matching does.
            # Without a hash the service can't link it either.
            kosync_doc_id = None
            if top.get('source') == 'Local File':
                kosync_doc_id = get_kosync_id_for_ebook(
                    top.get('ebook_filename') or '', source_path=top.get('source_path'),
                )
                if not kosync_doc_id:
                    logger.warning(
                        f"⚠️ Auto-match could not hash Local File '{top.get('ebook_filename')}' "
                        f"for '{suggestion.get('abs_title')}'; left for review"
                    )
                    remaining.append(suggestion)
                    continue
                # Saving a mapping absorbs any other book on the same hash. With
                # two audiobooks of one ebook, or two files sharing a filename
                # hash, the second match deletes the first one's mapping.
                audio_source_id = suggestion.get('audio_source_id') or suggestion.get('abs_id') or ''
                holder = database_service.get_book_by_kosync_id(kosync_doc_id)
                if (
                    holder is not None
                    and holder.sync_mode != 'ebook_only'
                    and (holder.audio_source_id or holder.abs_id) != audio_source_id
                ):
                    logger.info(
                        f"Auto-match skipped '{suggestion.get('abs_title')}': Local File "
                        f"'{top.get('ebook_filename')}' is already linked to another audiobook "
                        f"('{holder.abs_title}', {holder.abs_id}); left for review"
                    )
                    remaining.append(suggestion)
                    continue
                # Absorbing moves the holder's readers and progress, so unattended
                # auto-match only folds in the scanning reader's own mapping.
                document = database_service.get_kosync_document(kosync_doc_id)
                document_owner = getattr(document, 'linked_abs_id', None) if document else None
                other_readers = (
                    holder is not None and holder.sync_mode == 'ebook_only'
                    and any(uid != user_id for uid in database_service.get_book_user_ids(holder.abs_id))
                )
                if other_readers or document_owner not in (
                    None, suggestion.get('abs_id'), getattr(holder, 'abs_id', None),
                ):
                    logger.info(
                        f"Auto-match skipped '{suggestion.get('abs_title')}': Local File "
                        f"'{top.get('ebook_filename')}' belongs to another reader's mapping; "
                        f"left for review"
                    )
                    remaining.append(suggestion)
                    continue
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
                kosync_doc_id=kosync_doc_id,
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
        if (getattr(saved, 'audio_source', None) == 'BookOrbit'
                and env_truthy('SUGGESTIONS_AUTO_MATCH_READALONG')):
            try:
                _record_readalong_intent_for_match(saved.abs_id)
                logger.info("📖 Auto-match: read-along requested for '%s'", suggestion.get('abs_title'))
            except Exception as e:
                logger.warning(
                    "Auto-match: could not request a read-along for '%s': %s",
                    suggestion.get('abs_title'), e, exc_info=True,
                )
        try:
            _shelve_saved_ebook(saved)
        except Exception as shelf_err:
            logger.warning(
                f"Failed to shelve auto-matched ebook for '{suggestion.get('abs_title')}': {shelf_err}",
                exc_info=True,
            )

    if matched:
        results['suggestions'] = remaining
        results['cache_by_abs'] = cache_by_abs
        stats = results.get('stats') or {}
        stats['auto_matched'] = matched
        results['stats'] = stats
        logger.info(f"🔗 Auto-match created {matched} mapping(s) at or above {threshold:.1f}%")
    return results


def _run_suggestions_scan_job(job_id, cached_suggestions_by_abs=None, cached_no_match_abs_ids=None, full_refresh=False):
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
            full_refresh=full_refresh,
        )
        results = results if isinstance(results, dict) else {}
        complete = results.get("discovery_complete", True)
        if complete:
            results = _auto_match_suggestions(results, get_current_user_id())
            scanned_at = time.time()
            save_payload = {
                "scan_cache_by_abs": results.get('cache_by_abs', {}),
                "scan_cache_no_match_abs_ids": results.get('no_match_abs_ids', []),
                "scan_last_stats": results.get('stats', {}),
                "scan_generation": results.get('scan_generation'),
                "scanned_at": scanned_at,
            }
        else:
            scanned_at = results.get("scanned_at", _load_persisted_suggestions_cache().get("scanned_at", 0.0))
            save_payload = {"scanned_at": scanned_at}
            if results.pop("_exclude_setting_changed", False):
                save_payload.update({
                    "scan_cache_by_abs": results.get("cache_by_abs", {}),
                    "scan_cache_no_match_abs_ids": results.get("no_match_abs_ids", []),
                })
        if isinstance(results.get("ebook_discovery"), dict):
            save_payload["ebook_discovery"] = results["ebook_discovery"]
        snapshot = results.get("ebook_discovery") or {}
        save_payload["scan_last_attempt"] = {
            key: snapshot.get(key) for key in ("state", "attempted_at", "source_states", "error")
        }
        if _save_persisted_suggestions_cache(save_payload) is False:
            raise OSError("could not persist suggestions scan cache")
        status = "done"
        error = None
    except Exception as e:
        logger.exception(f"Suggestions scan job failed ({job_id}): {e}")
        results = {}
        scanned_at = 0.0
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
                "scanned_at": scanned_at,
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


def _running_suggestions_scan_job_id(user_id: Optional[str]) -> Optional[str]:
    """Return the job_id of a queued or running suggestions scan for the given user, if any."""
    if not user_id:
        return None
    with SUGGESTIONS_SCAN_JOBS_LOCK:
        for job_id, job in SUGGESTIONS_SCAN_JOBS.items():
            if job.get('user_id') == user_id and job.get('status') in _SCAN_JOB_ACTIVE:
                return job_id
    return None


def _owned_suggestions_scan_job(job_id):
    """The scan job ``job_id`` if it belongs to the acting reader, else None.

    A job id comes from the session, and a finished job's results are written into
    the reader's saved state, so a job started for someone else must never be read.
    """
    job = _get_suggestions_scan_job(job_id)
    if job is None or job.get('user_id') != get_current_user_id():
        return None
    return job


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
        "scanned_at": 0.0,
        "scan_generation": None,
        "ebook_discovery": {},
        "ebook_ignored": {},
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


def _empty_suggestions_cache_payload(scanned_at=0.0):
    return {
        "scan_cache_by_abs": {},
        "scan_cache_no_match_abs_ids": [],
        "scan_last_stats": {},
        "scan_generation": None,
        "ebook_discovery": {
            "schema_version": 1, "state": "never_scanned", "generation": None,
            "completed_at": None, "attempted_at": 0.0, "entries": {},
            "audio_fingerprints_by_key": {}, "stats": {}, "source_states": {},
        },
        "ebook_ignored": {},
        "scan_last_attempt": {},
        "scanned_at": scanned_at,
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
        scanned_at = raw.get('scanned_at', 0.0)
        scan_generation = raw.get('scan_generation')
        discovery = raw.get('ebook_discovery', {})
        ignored = raw.get('ebook_ignored', {})

        payload['scan_cache_by_abs'] = cache_by_abs if isinstance(cache_by_abs, dict) else {}
        payload['scan_cache_no_match_abs_ids'] = no_match if isinstance(no_match, list) else []
        payload['scan_last_stats'] = stats if isinstance(stats, dict) else {}
        payload['scan_generation'] = scan_generation if isinstance(scan_generation, str) else None
        if isinstance(discovery, dict) and isinstance(discovery.get('entries', {}), dict):
            discovery = dict(discovery)
            discovery['audio_fingerprints_by_key'] = (
                discovery.get('audio_fingerprints_by_key')
                if isinstance(discovery.get('audio_fingerprints_by_key'), dict) else {}
            )
            discovery['source_states'] = (
                discovery.get('source_states')
                if isinstance(discovery.get('source_states'), dict) else {}
            )
            discovery['stats'] = discovery.get('stats') if isinstance(discovery.get('stats'), dict) else {}
            payload['ebook_discovery'] = discovery
        payload['ebook_ignored'] = ignored if isinstance(ignored, dict) else {}
        payload['scan_last_attempt'] = raw.get('scan_last_attempt') if isinstance(raw.get('scan_last_attempt'), dict) else {}
        payload['scanned_at'] = scanned_at if isinstance(scanned_at, (int, float)) else 0.0

    return payload


def _save_persisted_suggestions_cache(payload, *, replace_ignored=False):
    cache_file = _suggestions_cache_file_path()
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = payload if isinstance(payload, dict) else {}
    temp_file = cache_file.with_suffix('.tmp')

    with SUGGESTIONS_CACHE_LOCK:
        try:
            current = {}
            if cache_file.exists():
                try:
                    loaded = json.loads(cache_file.read_text(encoding='utf-8'))
                    current = loaded if isinstance(loaded, dict) else {}
                except Exception as exc:
                    logger.warning("Could not read existing suggestions cache before saving (%s)", type(exc).__name__, exc_info=True)

            safe_payload = {
                "scan_cache_by_abs": current.get("scan_cache_by_abs", {}) if isinstance(current.get("scan_cache_by_abs", {}), dict) else {},
                "scan_cache_no_match_abs_ids": current.get("scan_cache_no_match_abs_ids", []) if isinstance(current.get("scan_cache_no_match_abs_ids", []), list) else [],
                "scan_last_stats": current.get("scan_last_stats", {}) if isinstance(current.get("scan_last_stats", {}), dict) else {},
                "scan_generation": current.get("scan_generation") if isinstance(current.get("scan_generation"), str) else None,
                "ebook_discovery": current.get("ebook_discovery", {}) if isinstance(current.get("ebook_discovery", {}), dict) else {},
                "ebook_ignored": current.get("ebook_ignored", {}) if isinstance(current.get("ebook_ignored", {}), dict) else {},
                "scanned_at": current.get("scanned_at", 0.0) if isinstance(current.get("scanned_at", 0.0), (int, float)) else 0.0,
                "scan_last_attempt": current.get("scan_last_attempt", {}),
                "updated_at": time.time(),
            }
            for key, expected_type in (
                ("scan_cache_by_abs", dict),
                ("scan_cache_no_match_abs_ids", list),
                ("scan_last_stats", dict),
                ("scan_last_attempt", dict),
            ):
                if isinstance(payload.get(key), expected_type):
                    safe_payload[key] = payload[key]
            if isinstance(payload.get("scanned_at"), (int, float)):
                safe_payload["scanned_at"] = payload["scanned_at"]
            if isinstance(payload.get("scan_generation"), str):
                safe_payload["scan_generation"] = payload["scan_generation"]

            discovery = payload.get("ebook_discovery")
            if (
                isinstance(discovery, dict)
                and isinstance(discovery.get("entries", {}), dict)
                and discovery.get("state") in {"complete", "incomplete"}
            ):
                safe_payload["ebook_discovery"] = discovery

            incoming_ignored = payload.get("ebook_ignored")
            if replace_ignored and isinstance(incoming_ignored, dict):
                safe_payload["ebook_ignored"] = incoming_ignored

            discovery = safe_payload["ebook_discovery"]
            if discovery.get("state") == "complete" and safe_payload["ebook_ignored"]:
                from src.services.ebook_discovery import ebook_identity_keys

                ignored = safe_payload["ebook_ignored"]
                ignored_keys = set(ignored)
                for record in ignored.values():
                    if isinstance(record, dict):
                        ignored_keys.update(ebook_identity_keys(record.get("entry") or record))
                entries = discovery.get("entries") or {}
                removed = []
                removed_by_classification = {"no_audio": 0, "has_audio": 0, "unknown": 0}
                for key, entry in entries.items():
                    if key in ignored_keys or ebook_identity_keys(entry) & ignored_keys:
                        removed.append(key)
                        classification = entry.get("classification")
                        if classification in removed_by_classification:
                            removed_by_classification[classification] += 1
                for key in removed:
                    entries.pop(key, None)
                stats = discovery.get("stats") or {}
                discovery["stats"] = stats
                if isinstance(stats.get("inventory_total"), int):
                    stats["inventory_total"] = max(0, stats["inventory_total"] - len(removed))
                for classification, count in removed_by_classification.items():
                    if isinstance(stats.get(classification), int):
                        stats[classification] = max(0, stats[classification] - count)

            temp_file.write_text(json.dumps(safe_payload, ensure_ascii=False), encoding='utf-8')
            temp_file.replace(cache_file)
            return True
        except Exception as e:
            logger.warning("Could not persist suggestions cache file (%s)", type(e).__name__, exc_info=True)
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass
            return False


def _rehydrate_suggestions_state_from_cache(suggestions_state: dict) -> dict:
    """Rehydrate in-memory suggestions state from the persisted per-user cache.

    If the state already has scan results (non-empty scan_cache_by_abs), the
    in-memory state wins unless the persisted cache comes from a newer scan
    (its scanned_at is ahead of the state's), as after a scheduled scan.
    Otherwise, loads the persisted cache and populates the state with its
    contents, including rebuilding scan_results sorted by top match score
    descending.
    """
    if not suggestions_state:
        return suggestions_state

    persisted = _load_persisted_suggestions_cache()
    suggestions_state['ebook_discovery'] = persisted.get('ebook_discovery', {})
    suggestions_state['ebook_ignored'] = persisted.get('ebook_ignored', {})
    suggestions_state['scan_generation'] = persisted.get('scan_generation')
    newer_scan = persisted['scanned_at'] > suggestions_state.get('scanned_at', 0.0)
    if suggestions_state.get('scan_cache_by_abs') and not newer_scan:
        return suggestions_state

    cache_by_abs = persisted.get('scan_cache_by_abs', {}) or {}
    if not cache_by_abs and not newer_scan:
        return suggestions_state

    suggestions_state['scan_cache_by_abs'] = cache_by_abs
    suggestions_state['scan_cache_no_match_abs_ids'] = persisted.get('scan_cache_no_match_abs_ids', []) or []
    suggestions_state['scan_last_stats'] = persisted.get('scan_last_stats', {}) or {}
    suggestions_state['scanned_at'] = persisted['scanned_at']
    suggestions_state['scan_has_run'] = bool(cache_by_abs or suggestions_state['scan_last_stats'])
    suggestions_state['updated_at'] = time.time()

    suggestions_list = list(cache_by_abs.values())
    suggestions_list.sort(
        key=lambda s: (s.get('matches', [{}])[0].get('score', 0) if s.get('matches') else 0),
        reverse=True,
    )
    suggestions_state['scan_results'] = suggestions_list

    logger.info(f"♻️ Restored {len(suggestions_list)} cached suggestion(s) from the persisted scan cache")
    return suggestions_state


def _persist_suggestions_state(suggestions_state: dict) -> None:
    with SUGGESTIONS_CACHE_LOCK:
        if _load_persisted_suggestions_cache()['scanned_at'] > suggestions_state.get('scanned_at', 0.0):
            return
        _save_persisted_suggestions_cache({
            "scan_cache_by_abs": suggestions_state.get('scan_cache_by_abs', {}),
            "scan_cache_no_match_abs_ids": suggestions_state.get('scan_cache_no_match_abs_ids', []),
            "scan_last_stats": suggestions_state.get('scan_last_stats', {}),
            "scan_generation": suggestions_state.get('scan_generation'),
            "scanned_at": suggestions_state.get('scanned_at', 0.0),
        })


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


def _write_match_queue_unlocked(items: list) -> bool:
    queue_file = _match_queue_file_path()
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": items if isinstance(items, list) else [], "updated_at": time.time()}
    temp_file = queue_file.with_suffix('.tmp')
    try:
        temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temp_file.replace(queue_file)
        return True
    except Exception as e:
        logger.warning(f"Could not persist match queue file '{queue_file}': {e}", exc_info=True)
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass
        return False


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


def _match_queue_move(queue_key: str, direction: str) -> str:
    """Reorder an actor-owned, still-staged item, preserving every other user's order.

    Moving a staged entry changes the durable Add Book queue. It therefore also
    changes the execution order *if* those items are submitted together later.
    Nothing already drained into a running batch can be moved here.
    """
    user_id, include_legacy = _match_queue_scope()
    if direction not in {'up', 'down', 'top'}:
        return 'missing'
    with MATCH_QUEUE_LOCK:
        items = _read_match_queue_unlocked()
        visible_positions = [
            idx for idx, item in enumerate(items)
            if _match_queue_item_visible(item, user_id, include_legacy)
        ]
        positions = [
            idx for idx in visible_positions
            if (items[idx].get('bridge_key') or items[idx].get('abs_id')) == queue_key
        ]
        if len(positions) > 1:
            return 'ambiguous'
        if not positions:
            return 'missing'
        start = visible_positions.index(positions[0])
        destination = {'up': max(0, start - 1),
                       'down': min(len(visible_positions) - 1, start + 1),
                       'top': 0}[direction]
        if destination == start:
            return 'unchanged'
        staged = [items[idx] for idx in visible_positions]
        staged.insert(destination, staged.pop(start))
        for idx, item in zip(visible_positions, staged):
            items[idx] = item
        _write_match_queue_unlocked(items)
    return 'moved'


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
            ui_template('_match_queue_panel.html'),
            queue=_load_match_queue(),
            storyteller_enabled=bool(_scan_user_clients().storyteller_client.is_configured()),
        )
    return redirect(url_for('suggestions'))


def _apply_queue_readalong_request(queue_items: list[dict]) -> None:
    """Mark BookOrbit-audio queue items for a read-along when the Suggestions
    queue's "Also generate read-along EPUBs" box was ticked at submit time."""
    if (request.form.get('readalong_epub_requested') or '').strip().lower() not in {'true', '1', 'yes', 'on'}:
        return
    for item in queue_items:
        if item.get('audio_source') == 'BookOrbit':
            item['readalong_epub_requested'] = True


def _suggestion_bulk_block(suggestion: dict) -> "str | None":
    """Why "Add all exact" leaves this suggestion for a person; None when it can be added."""
    from src.services.suggestions_service import SuggestionsService

    matches = suggestion.get('matches') or []
    if matches and is_comic_ebook_filename(matches[0].get('ebook_filename')):
        return 'comic'
    return SuggestionsService.bulk_ineligibility(suggestion)


def _queue_item_from_suggestion(suggestion: dict) -> "dict | None":
    """Build a batch-match queue item from a scanned suggestion's top match (the ebook
    BookBridge is most confident about). Used by bulk add ('add all exact' / 'add
    selected'); mirrors the item shape produced by the single add_to_queue handler.
    Storyteller tri-links stay a manual per-item choice, so storyteller_uuid is left blank."""
    matches = (suggestion or {}).get('matches') or []
    bridge_key = (suggestion or {}).get('bridge_key') or (suggestion or {}).get('abs_id')
    if not matches or not bridge_key:
        return None
    top = next((match for match in matches
                if not is_comic_ebook_filename(match.get('ebook_filename'))), None)
    if top is None:
        return None
    ebook_filename = top.get('ebook_filename') or ''
    if not ebook_filename:
        return None
    if _refuse_regular_user_local_selection(
        top.get('source'), top.get('source_path'), top.get('source_id'), ebook_filename,
    ):
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


from src.web.routes.suggestions import suggestions_page, suggestions_scan_status, get_suggestions, dismiss_suggestion, ignore_suggestion, clear_stale_suggestions




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

    if book.transcript_file and book.transcript_file != "DB_MANAGED":
        remove_owned(book.transcript_file, DATA_DIR / "transcripts")

    # Clean up audio cache directories (source tracks + WAV chunks from whisper transcription)
    other_ids = None if remaining_books is None else [b.abs_id for b in remaining_books]
    if not defer_audio_cache:
        errors = []
        for removed in remove_book_cache_dirs(DATA_DIR / "audio_cache", book.abs_id, other_ids=other_ids, errors=errors):
            logger.info(f"🗑️ Deleted audio cache: {removed}")
        for e in errors:
            logger.warning(f"⚠️ Failed to delete audio cache: {e}", exc_info=e)

    # Clean up full transcript directory (chapter JSON files + manifest)
    errors = []
    for removed in remove_book_cache_dirs(DATA_DIR / "transcripts" / "storyteller", book.abs_id, other_ids=other_ids, errors=errors):
        logger.info(f"🗑️ Deleted transcript directory: {removed}")
    for e in errors:
        logger.warning(f"⚠️ Failed to delete transcript directory: {e}", exc_info=e)

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
            shelf_assignments = (
                database_service.get_shelf_assignments(book.abs_id)
                if not is_bookorbit and not is_kavita else []
            )
            database_service.remove_pending_shelf_adds(book.abs_id, all_users=True)
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
            elif shelf_assignments:
                removed_targets = set()
                cleanup_succeeded = True
                for assignment in shelf_assignments:
                    source_id = assignment.get('source_id') or book.ebook_source_id
                    target = (assignment['target_user_id'], assignment['shelf_name'], source_id)
                    if target in removed_targets:
                        continue
                    removed_targets.add(target)
                    target_user_id, shelf_name, source_id = target
                    if target_user_id is None:
                        client = _global_clients.booklore_client
                    else:
                        try:
                            client = container.user_client_registry().get_clients(
                                target_user_id
                            ).booklore_client
                        except Exception:
                            cleanup_succeeded = False
                            continue
                    if (not client.is_configured()
                            or not client.remove_book_id_from_shelf(
                                source_id, shelf_name
                            )):
                        cleanup_succeeded = False
                if cleanup_succeeded:
                    database_service.remove_shelf_assignments(book.abs_id)
            elif shelf_add_waits_for_alignment(book):
                client, shelf_name = _aligned_shelf_target(get_current_user_id())
                if client is not None and client.is_configured():
                    client.remove_book_id_from_shelf(book.ebook_source_id, shelf_name)
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


_FOREIGN_MAPPING_MESSAGE = (
    "Forbidden: another reader's mapping already uses this book. Ask an admin to change it."
)


def _acting_user_may_modify_book(abs_id) -> bool:
    """_user_may_modify_book for whoever is acting right now.

    In a request that is the signed-in user; in a user-scoped background job
    (the Add Book queue) it is the user the job runs for. Work with no user at
    all is system work and may.
    """
    if has_request_context():
        return _user_may_modify_book(current_user(), abs_id)
    user_id = get_current_user_id()
    if user_id is None:
        return True
    try:
        user = database_service.get_user(user_id)
        if user is None:
            return False
        return bool(getattr(user, "is_admin", False)) or database_service.is_user_linked(user_id, abs_id)
    except Exception:
        return False


def _existing_mapping_access(existing_book, **identity) -> str:
    """Whether a match may rewrite ``existing_book`` for the acting user.

    'modify' when there is no such mapping or the user may change it. Otherwise
    the mapping is another reader's: 'join' when the request names the pairing it
    already has, so the caller only adds it to this reader's library, and 'deny'
    when the match would change it. ``identity`` takes the keyword arguments of
    _mapping_identity_changes.
    """
    if existing_book is None or _acting_user_may_modify_book(existing_book.abs_id):
        return "modify"
    changed, _ = _mapping_identity_changes(existing_book, **identity)
    if changed:
        logger.info(
            "🔒 Refused to change mapping '%s': it belongs to another reader (%s would change)",
            sanitize_log_data(existing_book.abs_id), ", ".join(changed),
        )
        return "deny"
    logger.info(
        "🔗 '%s' is already mapped by another reader with the same text — adding it to this reader's library unchanged",
        sanitize_log_data(existing_book.abs_id),
    )
    return "join"


def _may_absorb_mapping(abs_id) -> bool:
    """Whether a match may fold the mapping ``abs_id`` into another one."""
    if not abs_id or _acting_user_may_modify_book(abs_id):
        return True
    logger.info(
        "🔒 Refused to absorb mapping '%s': it belongs to another reader",
        sanitize_log_data(abs_id),
    )
    return False


def _may_replace_mapping(abs_id) -> bool:
    """Whether a match may overwrite the stored mapping ``abs_id`` wholesale (Forge)."""
    if not abs_id or database_service.get_book(abs_id) is None or _acting_user_may_modify_book(abs_id):
        return True
    logger.info(
        "🔒 Refused to change mapping '%s': it belongs to another reader (%s would change)",
        sanitize_log_data(abs_id), "the whole mapping",
    )
    return False


def _hash_owned_by_foreign_mapping(doc_hash, abs_id) -> bool:
    """Whether ``doc_hash`` belongs to another mapping the acting user may not modify."""
    doc = database_service.get_kosync_document(doc_hash)
    owner = getattr(doc, "linked_abs_id", None) if doc else None
    if not owner:
        owner = getattr(database_service.get_book_by_kosync_id(doc_hash), "abs_id", None)
    if not owner or owner == abs_id or _acting_user_may_modify_book(owner):
        return False
    logger.info(
        "ðŸ”’ Refused to move KoSync document '%s' from mapping '%s': it belongs to another reader",
        sanitize_log_data(doc_hash), sanitize_log_data(owner),
    )
    return True


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


from src.web.routes.me import api_me_kosync_documents, api_me_link_kosync_document, api_me_unlink_kosync_document, api_me_delete_kosync_document, api_me_books




def _get_visible_kosync_document_or_response(doc_hash: str):
    doc = database_service.get_kosync_document(doc_hash)
    if not doc:
        return None, (jsonify({"success": False, "error": "This KOSync document is no longer available."}), 404)
    user = current_user()
    claimed_abs_ids = _current_user_claimed_abs_ids(user)
    if not _kosync_document_visible_to_user(doc, user, claimed_abs_ids):
        return None, (jsonify({"success": False, "error": "You do not have permission to change this document."}), 403)
    return doc, None








def _delete_or_unlink_book(user, abs_id, book) -> None:
    """Shared catalog: drop the user's claim (+ their progress) when other users
    still claim the book; otherwise fully delete it."""
    claimants = database_service.get_book_user_ids(abs_id)
    if user is not None and user.id in claimants and len(claimants) > 1:
        database_service.unlink_user_book(user.id, abs_id)
        database_service.delete_states_for_book(abs_id, user_id=user.id)
        database_service.remove_pending_shelf_adds(abs_id, user.id)
        return
    worker_cancelled = manager.cancel_background_job(abs_id)
    database_service.delete_book(abs_id)
    cleanup_mapping_resources(book, defer_audio_cache=worker_cancelled)










# Human-readable labels for the `Job.stage` values `readalong_builder.py` /
# `readalong_delivery.py` report via the progress callback below. Falls back
# to the raw stage string (or "Working") for anything not in this map --
# never an error, since a future stage this map hasn't been updated for
# should still show *something* rather than break the status poll.
_READALONG_STAGE_LABELS = {
    "queued": "Queued",
    "resolving_audio": "Resolving audio",
    "converting_epub": "Converting EPUB 2 to EPUB 3",
    "parsing_epub": "Parsing EPUB",
    "transcoding_audio": "Transcoding audio",
    "building_overlays": "Building overlays",
    "packaging": "Packaging",
    "delivering": "Delivering to BookOrbit",
}


def _readalong_stage_label(stage: Optional[str]) -> str:
    """Human-readable label for a read-along job's raw ``Job.stage`` value.

    Anything that is not a non-empty string (``None`` -- no stage recorded
    yet, or a pre-migration row) falls back to "Working" rather than raising
    or showing a blank/placeholder value in the UI.
    """
    if not isinstance(stage, str) or not stage:
        return "Working"
    return _READALONG_STAGE_LABELS.get(stage, stage)


def _readalong_epub_worker(abs_id: str, job_id: Optional[int] = None) -> None:
    """Background worker: generate (Phases 1-4) and deliver (Phase 5) a
    read-along EPUB for one book.

    Runs off the request thread via `_spawn_user_background`, which has
    already rebound the triggering user's contextvars before calling this --
    BookOrbit is configured per user, and a bare `threading.Thread` would
    silently resolve the admin/global client instead (CLAUDE.md failure mode
    #5). Progress/outcome are persisted through the existing `Job` model (the
    same mechanism `_record_forge_match_job` uses) so
    `/api/readalong-epub/<abs_id>/status` can report the real result even
    after the triggering request has long since returned.

    Never touches `book.ebook_filename` / `book.original_ebook_filename` --
    `deliver_readalong_epub` itself is the one place that would, and it does
    not (see its own docstring).

    **Stage progress**: `deliver_readalong_epub`/`build_readalong_epub` call
    back into `_report_progress` below at each real stage transition
    (resolving the audio entry, converting EPUB 2 -> 3, parsing, the
    dominant ffmpeg transcode, building overlays, packaging, delivering) so
    `/status` can show real signal instead of a static "generating" message
    for however many minutes a long book takes. A failed progress *write* is
    logged and swallowed here (and
    again, defensively, inside the callback chain itself via
    `readalong_builder._safe_progress`) -- it must never be the reason a
    book fails to generate.

    ``job_id`` binds every status write to this worker's own row. It is
    optional because Forge records the row on a best-effort basis; when row
    creation fails, status writes are skipped rather than guessed by recency.
    """
    def _update_job(**kwargs) -> None:
        if job_id is None:
            return
        try:
            database_service.update_job_by_id(job_id, **kwargs)
        except Exception as e:
            logger.warning(
                "Could not record read-along job state for '%s' (job_id=%s, %s): %s",
                sanitize_log_data(abs_id), job_id, kwargs, e, exc_info=True,
            )

    def _report_progress(stage: str, fraction: float) -> None:
        _update_job(progress=fraction, stage=stage)

    try:
        book = database_service.get_book(abs_id)
        if not book:
            _update_job(last_error="Book no longer exists")
            return

        alignment_service = getattr(manager, "alignment_service", None) if manager else None
        if alignment_service is None:
            _update_job(last_error="Alignment service unavailable")
            return

        clients = uc()
        ebook_sync_client = clients.sync_clients.get("BookOrbit")
        audio_sync_client = clients.sync_clients.get("BookOrbitAudio")
        if audio_sync_client is None:
            _update_job(last_error="BookOrbit audio sync is not available")
            return

        result = deliver_readalong_epub(
            parser=container.ebook_parser(),
            alignment_service=alignment_service,
            bookorbit_client=clients.bookorbit_client,
            ebook_sync_client=ebook_sync_client,
            audio_sync_client=audio_sync_client,
            book=book,
            progress_callback=_report_progress,
        )
        if result is None:
            _update_job(
                last_error="Read-along generation was refused -- see server logs for the exact reason.",
            )
            return

        if not result.confirmed:
            _update_job(
                progress=1.0,
                last_error=(
                    "Generated and delivered, but BookOrbit has not confirmed it "
                    f"enabled (last status: {result.read_aloud_sync})."
                ),
            )
            logger.warning(
                "⚠️ Read-along EPUB delivered for '%s' but not confirmed enabled",
                sanitize_log_data(abs_id),
            )
            return

        _update_job(progress=1.0, last_error=None)
        logger.info(
            "📖 Read-along EPUB ready for %s", sanitize_log_data(book.abs_title or abs_id)
        )
    except Exception as e:
        logger.error(
            "❌ Read-along generation failed for '%s': %s", sanitize_log_data(abs_id), e, exc_info=True,
        )
        _update_job(last_error=f"Unexpected error: {e}")
    finally:
        # Manual and post-forge requests share this process-wide reservation.
        # The post-forge wrapper releases only when it cannot hand work here.
        _release_readalong_generation(abs_id)


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




def _get_stats_timezone():
    tz_name = os.environ.get("TZ", "America/New_York") or "America/New_York"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        get_persistent_condition_logger().warn(
            logger,
            f"invalid-tz:{tz_name}",
            "Invalid TZ '%s' for stats, falling back to America/New_York",
            tz_name,
            exc_info=True,
        )
        return ZoneInfo("America/New_York")


def _build_stats_cache_key():
    try:
        user = current_user()
    except RuntimeError:
        user = None
    user_id = user.id if user is not None else "anon"
    return f"stats::{user_id}::{os.environ.get('TZ', 'America/New_York')}"


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


# The stats page draws the 53 weeks ending this week. One extra day covers a
# browser whose date is behind the server's.
_HEATMAP_DAYS = 371


def _heatmap_window(today):
    """First and last date the stats heatmap can show."""
    return today - timedelta(days=_HEATMAP_DAYS), today


def _heatmap_from_mapping(days_map, start_date, end_date):
    days_map = days_map if isinstance(days_map, dict) else {}
    first, last = start_date.isoformat(), end_date.isoformat()
    heatmap = []
    for key, value in days_map.items():
        if first <= str(key)[:10] <= last:
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


def _abs_finished_books(progress, items, tz, abs_client=None):
    """Books ABS marks finished, newest first, with titles from the listening stats items."""
    progress = progress if isinstance(progress, dict) else {}
    items = items if isinstance(items, dict) else {}
    titles = {}
    finished = []
    for item_id, entry in progress.items():
        # Podcast episodes have their own progress entries; only whole books count.
        if not isinstance(entry, dict) or not entry.get("isFinished") or entry.get("episodeId"):
            continue
        try:
            finished_dt = datetime.fromtimestamp(float(entry["finishedAt"]) / 1000, tz)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        details = None
        # ABS keeps ebook reading in the same progress records, with no duration. So does an
        # audiobook marked finished unplayed, so only the item says whether it has audio.
        if abs_client is not None and item_id not in items and not float(entry.get("duration") or 0) > 0:
            details = abs_client.get_item_details(item_id)
            media = (details or {}).get("media") or {}
            if details is not None and not (media.get("audioFiles") or float(media.get("duration") or 0) > 0):
                continue
        metadata = (items.get(item_id) or {}).get("mediaMetadata") or {}
        title = metadata.get("title")
        if not title:
            if not titles:
                titles = {book.abs_id: book.abs_title for book in database_service.get_all_books()}
            title = titles.get(item_id)
        authors = metadata.get("authors") or []
        # A book marked finished without a listening session: only ABS knows its title.
        if not title and abs_client is not None:
            if details is None:
                details = abs_client.get_item_details(item_id)
            metadata = ((details or {}).get("media") or {}).get("metadata") or {}
            title = metadata.get("title")
            authors = metadata.get("authors") or authors
        author = ", ".join(a.get("name") for a in authors if isinstance(a, dict) and a.get("name"))
        finished.append({
            "bookKey": f"abs:{item_id}", "absId": item_id,
            "title": title or "Unknown book", "author": author or None,
            "finishedAt": int(finished_dt.timestamp()),
            "finishedDate": finished_dt.date().isoformat(), "month": finished_dt.month,
        })
    finished.sort(key=lambda book: book["finishedAt"], reverse=True)
    return finished


def _build_listening_yearly_recap(all_days, year, finished_books, tz):
    """Year-in-review for listening: monthly hours from the ABS daily map, and the
    books ABS marks finished that year."""
    months = [{"month": m, "seconds": 0, "pages": 0, "finished": 0} for m in range(1, 13)]
    finish_years = {
        book["absId"]: datetime.fromtimestamp(book["finishedAt"], tz).year for book in (finished_books or [])
    }
    finished_books = [book for book in (finished_books or []) if finish_years[book["absId"]] == year]
    for book in finished_books:
        months[book["month"] - 1]["finished"] += 1
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
        "booksFinished": len(finished_books),
        "finishedBooks": finished_books,
        # A book marked finished in a year with no listening time still needs its year listed.
        "availableYears": sorted(set(_years_from_days(all_days)) | set(finish_years.values())),
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
        listen_sessions = int(stats.get("listen_session_count") or 0)
        result.append({
            "bookKey": f"abs:{abs_id}",
            "absId": abs_id,
            "isLinked": True,
            "title": getattr(book, "abs_title", None) or stats.get("book_title") or "Unknown book",
            "author": None,
            "totalSeconds": listen_seconds,
            "sessionCount": listen_sessions,
            "avgSessionSeconds": listen_seconds // listen_sessions if listen_sessions else 0,
            "lastReadAt": int(stats["last_session_time"]) if stats.get("last_session_time") else None,
        })
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _abs_last_listened(abs_client, item_ids, per_page=100, max_pages=10):
    """Epoch seconds of each item's newest ABS listening session.

    ABS returns sessions newest first, so paging stops once every item has a date.
    Progress ``lastUpdate`` is no substitute: the bridge moves it when a reader leads.
    """
    wanted = set(item_ids or ())
    found = {}
    for page in range(max_pages):
        if not wanted - found.keys():
            break
        sessions = abs_client.get_listening_sessions(limit=per_page, page=page)
        if not isinstance(sessions, list):
            break
        for session in sessions:
            item_id = session.get("libraryItemId") if isinstance(session, dict) else None
            if item_id in wanted and item_id not in found and session.get("updatedAt"):
                found[item_id] = int(session["updatedAt"]) // 1000
        if len(sessions) < per_page:
            break
    return found


def _abs_listening_book_list(items, last_listened, titles):
    """Per-book rollup from ABS's own listening time, so the rows add up to its totals.

    ``last_listened`` maps item ids to epoch seconds and ``titles`` maps BookBridge
    abs_ids to titles. ABS gives no per-book session count.
    """
    last_listened = last_listened or {}
    result = []
    for item_id, item in (items or {}).items():
        total_seconds = int(round(float((item or {}).get("timeListening") or 0)))
        if total_seconds <= 0:
            continue
        metadata = item.get("mediaMetadata") or {}
        authors = metadata.get("authors") or []
        author = ", ".join(a.get("name") for a in authors if isinstance(a, dict) and a.get("name"))
        result.append({
            "bookKey": f"abs:{item_id}",
            "absId": item_id,
            "isLinked": item_id in titles,
            "title": metadata.get("title") or titles.get(item_id) or "Unknown book",
            "author": author or None,
            "totalSeconds": total_seconds,
            "sessionCount": None,
            "avgSessionSeconds": None,
            "lastReadAt": last_listened.get(item_id),
        })
    result.sort(key=lambda item: int(item.get("lastReadAt") or 0), reverse=True)
    return result


def _abs_book_listening_detail(abs_client, abs_id, session_limit=40, max_pages=20):
    """One book's listening totals and newest-first sessions from ABS, or None if it has none.

    Matches the shape of ``get_book_listening_detail`` so the book detail can use either.
    """
    per_page = 100
    sessions = []
    for page in range(max_pages):
        data = abs_client.get_item_listening_sessions(abs_id, items_per_page=per_page, page=page)
        if not isinstance(data, dict):
            break
        batch = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]
        sessions.extend(batch)
        if len(batch) < per_page:
            break
    sessions = [s for s in sessions if float(s.get("timeListening") or 0) > 0]
    if not sessions:
        return None

    def fraction(session, key):
        duration = float(session.get("duration") or 0)
        return float(session.get(key) or 0) / duration if duration > 0 else None

    total_seconds = int(round(sum(float(s.get("timeListening") or 0) for s in sessions)))
    newest = sessions[0]
    newest_progress = fraction(newest, "currentTime")
    return {
        "totalSeconds": total_seconds,
        "sessionCount": len(sessions),
        "avgSessionSeconds": total_seconds // len(sessions),
        "progressPercent": round(min(newest_progress, 1.0) * 100, 1) if newest_progress is not None else None,
        "lastReadAt": int(newest.get("updatedAt") or 0) // 1000,
        "bookTitle": newest.get("displayTitle"),
        "sessions": [{
            "startTime": int(s.get("startedAt") or 0) // 1000,
            "endTime": int(s.get("updatedAt") or 0) // 1000,
            "durationSeconds": int(round(float(s.get("timeListening") or 0))),
            "startProgress": fraction(s, "startTime"),
            "endProgress": fraction(s, "currentTime"),
        } for s in sessions[:session_limit]],
    }


def _local_listening_stats(tz):
    """ABS-shaped listening stats from the bridge's own AUDIOBOOK sessions, or None if empty."""
    days = database_service.get_listening_days(getattr(tz, "key", str(tz)))
    if not days:
        return None
    return {
        "days": days,
        "totalTime": sum(days.values()),
        "items": {
            abs_id: stats for abs_id, stats in database_service.get_all_reading_stats().items()
            if int(stats.get("listen_seconds") or 0) > 0
        },
    }


def _stats_abs_client():
    """The request user's own configured ABS client, or None. Never falls back to
    the global (primary admin) client when the user's bundle cannot be built."""
    try:
        abs_client = container.user_client_registry().get_clients(current_user().id).abs_client
        return abs_client if abs_client and abs_client.is_configured() else None
    except Exception as e:
        logger.warning("Stats: user ABS client unavailable, using local sessions: %s", e, exc_info=True)
        return None


def _build_listening_stats_payload(tz):
    abs_client = _stats_abs_client()
    use_abs = abs_client is not None
    if use_abs:
        raw_stats = abs_client.get_listening_stats()
    else:
        raw_stats = _local_listening_stats(tz)
    if not raw_stats:
        return None

    all_days = raw_stats.get("days") or {}
    daily = _recent_daily_from_mapping(all_days, tz, days=7)
    heatmap = _heatmap_from_mapping(all_days, *_heatmap_window(datetime.now(tz).date()))
    all_activity_dates = _activity_dates_from_daily([
        {"date": key, "seconds": value}
        for key, value in all_days.items()
    ])

    if use_abs:
        recent_sessions = raw_stats.get("recentSessions")
        if not isinstance(recent_sessions, list) or not recent_sessions:
            recent_sessions = abs_client.get_listening_sessions(limit=10)
        normalized_sessions = [
            _normalize_listening_session(session_data)
            for session_data in (recent_sessions or [])[:10]
            if isinstance(session_data, dict)
        ]
    else:
        normalized_sessions = database_service.get_recent_listening_sessions(10)

    summary = _summarize_activity(
        daily=daily,
        total_seconds=int(round(float(raw_stats.get("totalTime") or 0))),
        total_days=len(all_activity_dates),
        reference_date=datetime.now(tz).date(),
    )
    if use_abs:
        progress = abs_client.get_all_progress_raw()
        finished_books = _abs_finished_books(progress, raw_stats.get("items"), tz, abs_client)
        items = raw_stats.get("items") or {}
        books = _abs_listening_book_list(
            items, _abs_last_listened(abs_client, set(items)),
            {book.abs_id: book.abs_title for book in database_service.get_all_books()},
        )
    else:
        finished_books = database_service.get_listening_finished_books(getattr(tz, "key", str(tz)))
        books = _build_listening_book_list()
    summary["itemsFinished"] = len(finished_books)
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
        "books": books,
        "yearlyRecap": _build_listening_yearly_recap(all_days, datetime.now(tz).year, finished_books, tz),
    }


def _build_reading_stats_payload(tz):
    tz_name = getattr(tz, "key", str(tz))
    summary = database_service.get_koreader_dashboard_summary(tz_name)
    daily = database_service.get_koreader_daily_totals(7, tz_name)
    heatmap = database_service.get_koreader_heatmap(*_heatmap_window(datetime.now(tz).date()), tz_name)
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
            "finished": 0,
        })
    # A book finished as both an ebook and an audiobook counts once, at its first finish.
    finished_by_book = {}
    for book in list((reading_recap or {}).get("finishedBooks") or []) + list((listening_recap or {}).get("finishedBooks") or []):
        key = book.get("absId") or book.get("bookKey")
        if key not in finished_by_book or book["finishedAt"] < finished_by_book[key]["finishedAt"]:
            finished_by_book[key] = book
    finished_books = sorted(finished_by_book.values(), key=lambda book: book["finishedAt"], reverse=True)
    for book in finished_books:
        months[book["month"] - 1]["finished"] += 1
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


from src.web.routes.stats import (
    stats_view,
    api_stats,
    api_stats_reading_day,
    api_stats_reading_calendar,
    api_stats_book_detail,
    api_stats_yearly_recap,
)


from src.web.routes.status import api_status, api_status_progress


def _build_dashboard_progress_rows(books, all_states):
    """The per-book fields the dashboard's periodic refresh actually redraws.

    Derived from Book/State rows and scalar alignment status: no display-metadata
    resolution, no per-book service lookups, and above all no alignment map —
    which the full dashboard build loads per book to compute the drift badge
    (issue #412)."""
    states_by_book = _group_dashboard_states_by_book(all_states)
    ctc_aligned_book_ids = database_service.get_ctc_aligned_book_ids()
    readalong_ready_book_ids = database_service.get_readalong_ready_book_ids()
    sync_markers = database_service.get_sync_markers()
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

        last_slot_client, last_slot_unix = sync_markers.get(abs_id, (None, 0))
        rows.append({
            "abs_id": abs_id,
            "ctc_aligned": abs_id in ctc_aligned_book_ids,
            "readalong_ready": abs_id in readalong_ready_book_ids,
            "unified_progress": min(max_progress, 100.0),
            "last_sync": _format_dashboard_last_sync(latest_update_time),
            "last_sync_unix": latest_update_time,
            "last_slot": _format_strip_slot(last_slot_client),
            "last_slot_client": last_slot_client,
            "last_slot_service": _hero_leader_service(last_slot_client),
            "last_slot_unix": last_slot_unix,
            "last_slot_sync": _format_dashboard_last_sync(last_slot_unix),
            "states": states,
        })

    return rows




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




def _bookfusion_client_for_user(user_id):
    """Build a BookFusion client bound to a specific user so a device-link token
    persists to THAT user's credentials (used by the admin link-on-behalf flow)."""
    from src.api.bookfusion_client import BookFusionClient
    creds = database_service.get_user_credentials(user_id) or {}
    return BookFusionClient(credentials=creds, database_service=database_service, user_id=user_id)






def _serialize_bookfusion_search_item(item: dict) -> dict:
    """Normalize one BookFusion library search row for dashboard linking."""
    book_id = item.get("id") or item.get("book_id")
    return {
        "id": str(book_id) if book_id not in (None, "") else "",
        "title": str(item.get("title") or item.get("name") or "").strip(),
        "author": _coerce_author_display(item.get("authors") or item.get("author")),
    }








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




def _bookfusion_item_matches(item: dict, title: str, author: str = "") -> bool:
    """Return whether a User-API search result matches local EPUB metadata."""
    if not isinstance(item, dict):
        return False
    if (item.get("title") or "").strip().casefold() != title.strip().casefold():
        return False
    if not author:
        return True
    item_authors = item.get("authors") or item.get("author") or ""
    if isinstance(item_authors, list):
        first = item_authors[0] if item_authors else ""
        first_author = str(first.get("name") or first.get("authorName") or "").strip() if isinstance(first, dict) else str(first or "").strip()
    elif isinstance(item_authors, dict):
        first_author = str(item_authors.get("name") or item_authors.get("authorName") or "").strip()
    elif isinstance(item_authors, str):
        first_author = item_authors.strip()
    else:
        first_author = ""
    return first_author.casefold() == author.strip().casefold()


def _bookfusion_reader_id_from_url(item: dict) -> int | None:
    """Extract the Calibre/reader-facing id from a BookFusion User-API result."""
    read_url = str(item.get("read_url") or "")
    match = re.search(r"/books/(\d+)(?:[-/?#]|$)", read_url)
    if not match:
        return None
    return int(match.group(1))


def _find_bookfusion_user_api_id(results, title: str, author: str, expected_reader_id: int | None = None) -> int | None:
    """Find an exact metadata match, optionally tied to a Calibre reader id."""
    for item in results or []:
        if not _bookfusion_item_matches(item, title, author):
            continue
        if expected_reader_id is not None and _bookfusion_reader_id_from_url(item) != expected_reader_id:
            continue
        try:
            return int(item.get("id"))
        except (ValueError, TypeError):
            continue
    return None


def _resolve_created_bookfusion_id(calibre_book_id: int | None, title: str, author: str = "") -> int | None:
    """Resolve a newly-uploaded Calibre id to BookFusion's User-API id.

    The Calibre finalize endpoint returns the id embedded in ``read_url`` rather
    than the User API's ``id``. Search is retried briefly because uploads are
    indexed asynchronously; no link is saved until the exact title match (and
    author, when supplied) also exposes that returned reader id.
    """
    if calibre_book_id is None or not title:
        return None
    reader_client = uc().bookfusion_client
    if not reader_client.is_configured():
        return None
    for attempt in range(3):
        try:
            results = reader_client.search_books(page=1, per_page=10, q=title) or []
        except Exception as exc:
            logger.warning("BookFusion post-upload User API search failed: %s", exc, exc_info=True)
            return None
        resolved_id = _find_bookfusion_user_api_id(results, title, author, calibre_book_id)
        if resolved_id is not None:
            return resolved_id
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    return None


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
    for item in results:
        if not _bookfusion_item_matches(item, title, author):
            continue
        try:
            candidate_id = int(item.get("id"))
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
