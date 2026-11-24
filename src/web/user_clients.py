"""C03 ABS form and per-user client resolution, extracted unchanged from web_server."""

from importlib import import_module

_WEB_MODULE = None


def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def get_audiobooks_conditionally():
    """Get audiobooks either from specific library or all libraries based on ABS_ONLY_SEARCH_IN_ABS_LIBRARY_ID setting."""
    web = _web_server()
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

    abs_client = web.uc().abs_client
    if abs_library_id:
        # Fetch audiobooks only from the specified library
        return abs_client.get_audiobooks_for_lib(abs_library_id)
    else:
        # Fetch all audiobooks from all libraries
        return abs_client.get_all_audiobooks()


def _normalize_abs_form_value(key: str, raw_value) -> str:
    web = _web_server()
    clean_value = str(raw_value or "").strip()
    if not clean_value:
        return ""
    if key in {"ABS_SERVER", "ABS_KEY"} and web.is_abs_disabled_value(clean_value):
        return web.ABS_DISABLED_SENTINEL
    if key == "ABS_SERVER" and not clean_value.lower().startswith(("http://", "https://")):
        return f"http://{clean_value}"
    return clean_value


def _display_abs_server() -> str:
    web = _web_server()
    abs_server = web.os.environ.get("ABS_SERVER", "")
    if web.is_abs_disabled_value(abs_server):
        return ""
    return abs_server


class _UnavailableClient:
    def is_configured(self):
        return False


class _GlobalClients:
    """Fallback that exposes the global singletons under the same attribute names
    as a per-user bundle (used for unauthenticated/admin/global contexts)."""
    @property
    def abs_client(self):
        web = _web_server()
        return web.container.abs_client()
    @property
    def booklore_client(self):
        web = _web_server()
        return web.container.booklore_client()
    @property
    def bookfusion_client(self):
        web = _web_server()
        return web.container.bookfusion_client()
    @property
    def bookorbit_client(self):
        web = _web_server()
        return web.container.bookorbit_client()
    @property
    def kavita_client(self):
        web = _web_server()
        provider = getattr(web.container, "kavita_client", None)
        return provider() if provider else web._UNAVAILABLE_CLIENT
    @property
    def cwa_client(self):
        web = _web_server()
        return web.container.cwa_client()
    @property
    def storyteller_client(self):
        web = _web_server()
        return web.container.storyteller_client()
    @property
    def hardcover_client(self):
        web = _web_server()
        return web.container.hardcover_client()
    @property
    def storygraph_client(self):
        web = _web_server()
        return web.container.storygraph_client()
    @property
    def shelfmark_client(self):
        web = _web_server()
        return web.container.shelfmark_client()
    @property
    def library_service(self):
        web = _web_server()
        return web.container.library_service()
    @property
    def sync_clients(self):
        web = _web_server()
        return web.container.sync_clients()


def uc():
    """The active client bundle for this request: the logged-in user's own
    clients (per-user credentials/library) when available, else the global
    singletons. Use this in user-facing flows (match/forge/search) so they act
    on the user's library, not the admin's."""
    web = _web_server()
    override = web._active_bundle.get()
    if override is not None:
        return override
    try:
        user = web.current_user()
    except RuntimeError:
        user = None
    if user is None:
        # Background per-user work has no Flask request context. The sync
        # cycle/poller binds the ambient user id before invoking helpers such
        # as SuggestionsService, so resolve that user's bundle instead of
        # silently falling back to the admin/global clients.
        user_id = web.get_current_user_id()
        if user_id is not None:
            try:
                return web.container.user_client_registry().get_clients(user_id)
            except Exception as e:
                web.logger.debug("uc(): ambient user bundle unavailable: %s", e)
    if user is not None:
        try:
            return web.container.user_client_registry().get_clients(user.id)
        except Exception as e:
            web.logger.debug("uc(): falling back to global clients: %s", e)
    return web._global_clients


def _scan_user_clients():
    """uc() for Suggestions scans: a user who may not use the admin's clients gets an error, not them."""
    web = _web_server()
    clients = web.uc()
    if isinstance(clients, _GlobalClients):
        user_id = web.get_current_user_id()
        user = web.database_service.get_user(user_id) if user_id is not None and web.database_service is not None else None
        if user is not None and not web._global_fallback_allowed(web.database_service, user):
            raise PermissionError("user client bundle unavailable")
    return clients


def _acting_user_may_use_global_config() -> bool:
    """True when no user is acting or the acting user may use the admin's global clients."""
    web = _web_server()
    user_id = web.get_current_user_id()
    if user_id is None or web.database_service is None:
        return True
    user = web.database_service.get_user(user_id)
    return user is None or web._global_fallback_allowed(web.database_service, user)


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
