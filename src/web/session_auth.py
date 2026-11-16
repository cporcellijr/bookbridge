"""C02 helpers extracted from web_server with no behavior change."""

import hashlib
import hmac
from importlib import import_module

_WEB_MODULE = None

def _web_server():
    if _WEB_MODULE is not None:
        return _WEB_MODULE
    return import_module("src.web_server")


def session_marker(user) -> str:
    """Short HMAC of the user's password hash, keyed with the app secret.

    Stored in the session at login; a password change alters it, so every
    older session for that user stops matching in `current_user`.
    """
    key = _web_server().current_app.secret_key
    key = key.encode() if isinstance(key, str) else key
    return hmac.new(key, (user.password_hash or '').encode(), hashlib.sha256).hexdigest()[:16]


def current_user():
    """Return the logged-in User for this request (cached on g), or None."""
    web = _web_server()
    if 'current_user' in web.g.__dict__:
        return web.g.current_user
    user = None
    uid = web.session.get('user_id')
    if uid and web.database_service is not None:
        try:
            candidate = web.database_service.get_user(uid)
            if (
                candidate
                and candidate.active
                and hmac.compare_digest(
                    str(web.session.get('session_marker') or ''), session_marker(candidate)
                )
            ):
                user = candidate
        except Exception:
            user = None
    web.g.current_user = user
    return user


def admin_required(f):
    """Decorator for routes that require an admin user (e.g. user management)."""
    web = _web_server()
    @web.wraps(f)
    def wrapper(*args, **kwargs):
        if web.current_app.config.get('LOGIN_DISABLED'):
            return f(*args, **kwargs)
        user = web.current_user()
        if user is None:
            if web._request_wants_json():
                return web.jsonify({"error": "authentication required"}), 401
            return web.redirect(web.url_for('login', next=web.request.path))
        if not user.is_admin:
            return ("Forbidden: admin access required", 403)
        return f(*args, **kwargs)
    return wrapper


def _safe_next_url(default=None):
    """Resolve the post-auth redirect target, allowing only same-site relative
    paths. Absolute (`http://…`) and protocol-relative (`//host`, `/\\host`)
    values are rejected to prevent an open redirect, as are control characters:
    browsers drop a tab (`/\t/host` becomes `//host`) and CR/LF break headers."""
    web = _web_server()
    fallback = default or web.url_for('index')
    next_url = web.request.args.get('next') or fallback
    if (
        not next_url.startswith('/')
        or next_url.startswith('//')
        or next_url.startswith('/\\')
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in next_url)
    ):
        return fallback
    return next_url


def _trusted_proxy_networks() -> list:
    """Networks allowed to present the remote-auth header, read per call.

    Empty/unset means loopback only. Parsed per call so the Settings UI applies
    without a restart; malformed entries are dropped rather than silently
    widening (or emptying) the trust list.
    """
    web = _web_server()
    import ipaddress

    raw = web.os.environ.get('REMOTE_AUTH_TRUSTED_PROXIES', '') or ''
    entries = [part.strip() for part in raw.replace('\n', ',').split(',') if part.strip()]
    if not entries:
        entries = list(web._LOOPBACK_PROXY_NETWORKS)

    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            web.get_persistent_condition_logger().warn(
                web.logger,
                f"remote_auth_bad_proxy_entry:{entry}",
                "⚠️ REMOTE_AUTH_TRUSTED_PROXIES entry %s is not a valid IP or CIDR — ignoring it",
                web.sanitize_log_data(entry),
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
    web = _web_server()
    import ipaddress

    peer = (web.request.remote_addr or '').strip()
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in _trusted_proxy_networks())


def _client_address() -> str:
    """The dashboard client's address for login rate limiting.

    The TCP peer, unless the peer is a trusted proxy: then the right-most
    X-Forwarded-For hop that is not itself a trusted proxy. Otherwise every client
    behind a proxy shares one address and five bad passwords lock everyone out.
    """
    web = _web_server()
    import ipaddress

    networks = _trusted_proxy_networks()

    def trusted(address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in networks)

    peer = (web.request.remote_addr or '').strip()
    if not trusted(peer):
        return peer
    hops = [hop.strip() for hop in web.request.headers.get('X-Forwarded-For', '').split(',') if hop.strip()]
    for hop in reversed(hops):
        if not trusted(hop):
            return hop
    return peer


def _remote_auth_user():
    """Return the User named by the configured reverse-proxy header, or None."""
    web = _web_server()
    if not web.env_truthy('REMOTE_AUTH_ENABLED'):
        return None
    header_name = web.os.environ.get('REMOTE_AUTH_HEADER', 'Remote-User').strip() or 'Remote-User'
    username = (web.request.headers.get(header_name) or '').strip()
    if not username or web.database_service is None:
        return None
    if not _remote_auth_peer_trusted():
        # Someone reached the port directly and supplied the header. That is either
        # a misconfigured trust list or an attempt to bypass login, and both need to
        # be visible — but it can repeat on every request, so it is rate-limited.
        web.get_persistent_condition_logger().warn(
            web.logger,
            f"remote_auth_untrusted_peer:{web.request.remote_addr}",
            "🔒 Rejected %s header from untrusted address %s — add the reverse proxy "
            "to REMOTE_AUTH_TRUSTED_PROXIES if this is your proxy (empty = loopback only)",
            web.sanitize_log_data(header_name),
            web.sanitize_log_data(web.request.remote_addr or 'unknown'),
        )
        return None
    try:
        user = web.database_service.get_user_by_username(username)
    except Exception:
        return None
    if user is None or not user.active:
        return None
    return user


def _establish_session_and_redirect(user):
    """Set up an authenticated session for `user` and redirect to a safe next."""
    web = _web_server()
    web.session['user_id'] = user.id
    web.session['username'] = user.username
    web.session['role'] = user.role
    web.session['session_marker'] = session_marker(user)
    web.session.permanent = True
    try:
        web.database_service.touch_user_login(user.id)
    except Exception:
        pass
    return web.redirect(_safe_next_url())
