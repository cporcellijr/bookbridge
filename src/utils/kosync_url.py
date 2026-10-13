"""Detection of a KoSync URL that points at this bridge's own built-in server."""
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def is_builtin_kosync_url(url: str) -> bool:
    """True when ``url`` is a loopback address on the built-in KoSync port.

    A port-less URL takes its scheme's default port (80/443).
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False

    if (parsed.hostname or "").strip().lower() not in _LOCAL_HOSTS:
        return False

    valid_ports = {5757}
    configured_port = (os.environ.get("KOSYNC_PORT") or "").strip()
    if configured_port:
        try:
            valid_ports.add(int(configured_port))
        except ValueError:
            logger.warning(f"Invalid KOSYNC_PORT '{configured_port}' while checking for the built-in KoSync server", exc_info=True)

    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return port in valid_ports
