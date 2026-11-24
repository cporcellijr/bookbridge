"""C01 helpers extracted from web_server with no behavior change."""

import functools
import os
import re
from importlib import import_module
from typing import Optional

from werkzeug.security import safe_join

_WEB_MODULE = None
# A /static/ path with no query string yet; '#' keeps icons.svg#name. Starting on the literal
# (the opening quote is checked in the callback) keeps a 500 KB Library page near 1 ms.
_STATIC_URL = re.compile(r'''/static/([\w./-]+)(?=["'#)])''')
_GZIP_STATIC_TYPES = ('text/css', 'text/javascript', 'application/javascript', 'image/svg+xml', 'application/json')

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def _normalize_locale(value: Optional[str]) -> Optional[str]:
    """'ru-RU' / 'ru_RU' / 'ru' -> 'ru'; junk -> None."""
    web = _web_server()
    if not value:
        return None
    primary = str(value).strip().replace("_", "-").split("-")[0].lower()
    return primary or None


def locale_display_name(code: str) -> str:
    """'ru' -> 'Русский' (the language's name in itself); falls back to the code."""
    try:
        from babel import Locale

        return Locale.parse(code).get_display_name(code).capitalize()
    except Exception:
        return code


def get_locale():
    """Flask-Babel locale selector (passed to Babel(locale_selector=...))."""
    web = _web_server()
    try:
        user = web.current_user()
        if user is not None:
            wanted = _normalize_locale(getattr(user, "language", None))
            if wanted and wanted in web.available_locales():
                return wanted
    except Exception:
        pass
    try:
        if web.has_request_context():
            best = web.request.accept_languages.best_match(web.available_locales())
            if best:
                return best
    except Exception:
        pass
    return web._BASE_LOCALE


def _request_wants_json() -> bool:
    """True for API/XHR requests that should get a 401 instead of a redirect."""
    web = _web_server()
    if web.request.path.startswith('/api/'):
        return True
    accept = web.request.headers.get('Accept', '')
    if 'application/json' in accept and 'text/html' not in accept:
        return True
    return web.request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def inject_csrf_script(response):
    """after_request hook: embed the CSRF bootstrap into authenticated HTML
    pages so fetch()/form submits forward the per-session token automatically."""
    web = _web_server()
    try:
        if not web.current_app.config.get('CSRF_ENABLED', True):
            return response
        if web.session.get('user_id') is None:
            return response
        if response.status_code != 200 or response.mimetype != 'text/html':
            return response
        if response.direct_passthrough:
            return response
        body = response.get_data(as_text=True)
        idx = body.rfind('</body>')
        if idx == -1:
            return response
        snippet = web._CSRF_BOOTSTRAP_TEMPLATE.replace('__CSRF_TOKEN__', web._ensure_csrf_token())
        response.set_data(body[:idx] + snippet + body[idx:])
    except Exception as e:  # never let CSRF wiring break a page render
        web.logger.debug(f"CSRF inject skipped: {e}")
    return response


def gzip_html_response(response):
    """after_request hook: compress HTML pages and signed-in JSON for browsers that accept gzip.

    The Library page is about 500 KB of markup and compresses to a tenth of that in a
    few milliseconds, which matters on a phone. Discover's JSON (1.3 MB of reader
    documents) shrinks the same way. JSON is compressed only for a browser session:
    KoSync devices reach the same app without one and keep plain responses.
    """
    web = _web_server()
    try:
        if (
            response.status_code != 200
            or response.mimetype not in ('text/html', 'application/json')
            or (response.mimetype == 'application/json' and not web.session.get('user_id'))
            or response.direct_passthrough
            or response.is_streamed
            or 'Content-Encoding' in response.headers
            or web.request.accept_encodings.quality('gzip') <= 0
        ):
            return response
        body = response.get_data()
        if len(body) < 1024:
            return response
        response.set_data(web.gzip.compress(body, compresslevel=6))
        response.headers['Content-Encoding'] = 'gzip'
        response.vary.add('Accept-Encoding')
    except Exception as e:  # compression must never break a page
        web.logger.debug(f"Gzip skipped: {e}")
    return response


@functools.lru_cache(maxsize=1024)
def _static_version(static_dir: str, path: str) -> Optional[str]:
    """Size and mtime of a static file, read once per process (a deploy restarts it)."""
    full = safe_join(static_dir, path)
    try:
        stat = os.stat(full) if full else None
    except OSError:
        return None
    return f"{stat.st_size:x}-{stat.st_mtime_ns:x}" if stat else None


def _stamp_static_urls(text: str, static_dir: str) -> str:
    """Append ?v=<file version> to each quoted or url()-wrapped /static/ URL whose file exists."""
    def stamp(match):
        if match.string[match.start() - 1:match.start()] not in ('"', "'", '('):
            return match.group(0)
        version = _static_version(static_dir, match.group(1))
        return f"{match.group(0)}?v={version}" if version else match.group(0)
    return _STATIC_URL.sub(stamp, text)


def version_static_urls(response):
    """after_request hook: stamp each /static/ URL in an HTML page with its file version.

    A versioned URL is cached for a year (see static_asset_response), so the browser
    stops revalidating every stylesheet, script and icon on each navigation, and a
    changed file gets a new URL after the restart that deploys it.
    """
    web = _web_server()
    try:
        if (
            response.status_code != 200
            or response.mimetype != 'text/html'
            or response.direct_passthrough
            or response.is_streamed
            or 'Content-Encoding' in response.headers
        ):
            return response
        response.set_data(_stamp_static_urls(response.get_data(as_text=True), web.current_app.static_folder))
    except Exception as e:  # versioning must never break a page
        web.logger.debug(f"Static versioning skipped: {e}")
    return response


def static_asset_response(response):
    """after_request hook for /static/: cache a current versioned URL for a year, stamp
    the URLs inside stylesheets (fonts) and gzip text assets."""
    web = _web_server()
    if not web.request.path.startswith('/static/'):
        return response
    try:
        root = web.current_app.static_folder
        version = web.request.args.get('v')
        if version and response.status_code in (200, 304) and version == _static_version(root, web.request.path[len('/static/'):]):
            response.headers['Cache-Control'] = web._THUMB_IMMUTABLE_CACHE
        if response.status_code != 200 or response.mimetype not in _GZIP_STATIC_TYPES or 'Content-Encoding' in response.headers:
            return response
        response.direct_passthrough = False
        body = response.get_data()
        if response.mimetype == 'text/css':
            # shortcut: a stylesheet's own version ignores the files it names; rename a changed font instead of editing it in place.
            body = _stamp_static_urls(body.decode('utf-8'), root).encode('utf-8')
        if len(body) >= 1024 and web.request.accept_encodings.quality('gzip') > 0:
            body = web.gzip.compress(body, compresslevel=6)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers.pop('Accept-Ranges', None)
            response.vary.add('Accept-Encoding')
        response.set_data(body)
        etag, _weak = response.get_etag()
        if etag:
            response.set_etag(etag, weak=True)  # the body is no longer byte-identical to the file
    except Exception as e:  # caching and compression must never break a static file
        web.logger.debug(f"Static asset headers skipped: {e}")
    return response


def _bind_request_user_context(user):
    """Set the ambient per-user id + credentials for this request (reset in
    teardown). Tokens are stashed on g."""
    web = _web_server()
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY
    try:
        creds = web.database_service.get_user_credentials(user.id) if web.database_service else {}
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
    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = web._global_fallback_allowed(web.database_service, user)
    web.g._uctx_id_token = web.set_current_user_id(user.id)
    web.g._uctx_creds_token = web.set_current_user_credentials(creds)


def _release_request_user_context(_exc=None):
    web = _web_server()
    tok = web.g.pop('_uctx_id_token', None) if hasattr(web.g, 'pop') else None
    if tok is not None:
        web.reset_current_user_id(tok)
    tok = web.g.pop('_uctx_creds_token', None) if hasattr(web.g, 'pop') else None
    if tok is not None:
        web.reset_current_user_credentials(tok)


def ui_template(name: str) -> str:
    """Use a matching New-interface template for this request's reader, if present."""
    web = _web_server()
    if web.user_setting("UI_MODE", web.DEFAULT_CONFIG["UI_MODE"]) == "new":
        candidate = f"next/{name}"
        try:
            web.current_app.jinja_env.get_template(candidate)
        except web.TemplateNotFound:
            pass
        else:
            return candidate
    return name


# ---------------- CONTEXT PROCESSORS ----------------
def inject_global_vars():
    web = _web_server()
    def get_val(key, default_val=None):
        if key in web.os.environ: return web.os.environ[key]
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
        val = web.os.environ.get(key, 'false')
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
            return len(web._load_match_queue())
        except Exception:
            return 0

    return dict(
        shelfmark_url=web.os.environ.get("SHELFMARK_URL", ""),
        abs_server=web._display_abs_server(),
        booklore_server=web.os.environ.get("BOOKLORE_SERVER", ""),
        get_val=get_val,
        get_bool=get_bool,
        get_user_val=get_user_val,
        get_user_bool=get_user_bool,
        display_settings=lambda: web._display_settings_for(web.current_user()),
        match_queue_count=match_queue_count,
        current_user=web.current_user(),
    )
