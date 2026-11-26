"""Standalone provider connection checks used by Settings test actions."""

import os
import requests

from src.api.api_clients import is_abs_disabled_value
from src.utils.kosync_url import is_builtin_kosync_url
from src.utils.kosync_headers import kosync_request_kwargs


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


def _build_test_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


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

    if is_builtin_kosync_url(url):
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


def _test_bookorbit(enabled: bool, url: str, user: str, pwd: str, api_key: str = "") -> dict:
    if not enabled:
        return {"ok": False, "message": "BookOrbit is disabled"}
    if url and api_key:
        r = requests.get(
            f"{url}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if r.status_code == 200:
            body = r.json() if r.content else {}
            name = body.get("username") if isinstance(body, dict) else None
            return {"ok": True, "message": f"Authenticated with API key as {name}" if name else "Authenticated with API key"}
        if r.status_code in (401, 403):
            return {"ok": False, "message": "API key refused (revoked, expired, or API keys turned off in BookOrbit)"}
        return {"ok": False, "message": f"API key check returned {r.status_code}"}
    if not url or not user or not pwd:
        return {"ok": False, "message": "Missing URL, and username + password or API key"}
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


def _test_shelfmark(user: str, pwd: str) -> dict:
    """Check a Shelfmark login with one sign-in, using only the posted values (no admin fallback)."""
    from src.api.shelfmark_client import ShelfmarkClient
    from src.utils.user_config import _ALLOW_GLOBAL_FALLBACK_KEY

    if not os.environ.get("SHELFMARK_URL"):
        return {"ok": False, "message": "Shelfmark has no URL yet (set it in Settings)"}
    creds = {"SHELFMARK_USER": user, "SHELFMARK_PASSWORD": pwd, _ALLOW_GLOBAL_FALLBACK_KEY: False}
    ok, message = ShelfmarkClient(credentials=creds).check_connection()
    return {"ok": ok, "message": message}


def _test_storygraph(enabled: bool, session_cookie: str, remember_user_token: str) -> dict:
    from src.api.storygraph_client import normalize_cookie_value

    if not enabled:
        return {"ok": False, "message": "StoryGraph is disabled"}
    # Test what the sync client will send: a URL-encoded paste is decoded there too.
    session_cookie = normalize_cookie_value(session_cookie)
    remember_user_token = normalize_cookie_value(remember_user_token)
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


def _test_fable(enabled: bool, email: str, password: str) -> dict:
    """Test a Fable login by signing in, without posting anything."""
    from src.api.fable_client import FableClient

    if not enabled:
        return {"ok": False, "message": "Fable is disabled"}
    if not email or not password:
        return {"ok": False, "message": "Missing Fable email or password"}
    credentials = {
        "FABLE_ENABLED": "true",
        "FABLE_EMAIL": email,
        "FABLE_PASSWORD": password,
        "__allow_global_fallback__": False,
    }
    client = FableClient(credentials, user_id=-1, enforce_global_gate=False)
    try:
        client.check_connection()
    except RuntimeError:
        return {"ok": False, "message": "Fable sign-in failed; check the email and password"}
    return {"ok": True, "message": "Fable login accepted"}


def _test_goodreads(enabled: bool, cookie: str, refresher_url: str, token: str, browser_url: str = "") -> dict:
    """Test a Goodreads account without saving a cookie or posting progress."""
    from src.api.goodreads_client import GoodreadsClient

    if not enabled:
        return {"ok": False, "message": "Goodreads is disabled"}
    credentials = {
        "GOODREADS_ENABLED": "true",
        "GOODREADS_SESSION_COOKIE": cookie,
        "GOODREADS_COOKIE_REFRESH_URL": refresher_url,
        "GOODREADS_COOKIE_REFRESH_TOKEN": token,
        "GOODREADS_BROWSER_URL": browser_url,
        "__allow_global_fallback__": False,
    }
    client = GoodreadsClient(credentials, user_id=-1, enforce_global_gate=False)
    client.check_connection()
    return {"ok": True, "message": "Goodreads session accepted"}


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
