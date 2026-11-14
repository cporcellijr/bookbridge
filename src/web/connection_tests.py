"""Normalize connection-test inputs and dispatch HTTP responses."""

from flask import jsonify

from src.api.api_clients import ABS_DISABLED_SENTINEL, is_abs_disabled_value
from src.services.integration_connection_probes import (
    _test_conn_error, _test_llm_provider, _test_whispercpp,
    _test_abs, _test_kosync, _test_storyteller, _test_booklore,
    _test_bookorbit, _test_kavita, _test_cwa, _test_hardcover,
    _test_readest, _test_bookfusion, _test_shelfmark, _test_storygraph,
    _test_goodreads, _test_telegram,
)


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
            _coerce_test_str(data.get('BOOKORBIT_API_KEY')),
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
        'goodreads': lambda data: _test_goodreads(
            _coerce_test_bool(data.get('GOODREADS_ENABLED')),
            _coerce_test_str(data.get('GOODREADS_SESSION_COOKIE')),
            _coerce_test_str(data.get('GOODREADS_COOKIE_REFRESH_URL')),
            _coerce_test_str(data.get('GOODREADS_COOKIE_REFRESH_TOKEN')),
            _coerce_test_str(data.get('GOODREADS_BROWSER_URL')),
        ),
        'shelfmark': lambda data: _test_shelfmark(
            _coerce_test_str(data.get('SHELFMARK_USER')),
            _coerce_test_str(data.get('SHELFMARK_PASSWORD')),
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
