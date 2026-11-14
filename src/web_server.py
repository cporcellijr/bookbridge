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
