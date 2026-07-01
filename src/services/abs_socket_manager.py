"""ABS Socket.IO manager — supervises one listener per user (multi-user).

Audiobookshelf emits ``user_item_progress_updated`` only over the socket of the
user whose progress changed, so a single listener (authenticated as the admin)
never sees other users' playback. This manager starts one
:class:`ABSSocketListener` per active user that has their own ABS token, each
authenticated as that user and triggering that user's scoped sync cycle.

The global/admin listener (``user_id=None``) is always started when a global
``ABS_KEY`` is configured, preserving single-user behavior exactly. Per-user
listeners are only added for users whose resolved ABS token differs from the
global one, so an admin (whose token falls back to the global key) is not
double-listened.
"""

import logging
import os
import threading

from src.services.abs_socket_listener import ABSSocketListener
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)


class ABSSocketManager:
    """Starts and supervises per-user ABS Socket.IO listeners."""

    def __init__(self, database_service, sync_manager, user_client_registry=None):
        self._db = database_service
        self._sync_manager = sync_manager
        self._registry = user_client_registry
        self._listeners: list[ABSSocketListener] = []
        self._threads: list[threading.Thread] = []

    def _listener_targets(self) -> list[tuple]:
        """Return ``[(user_id, server_url, token)]`` for each listener to start.

        Always includes the global listener (``user_id=None``) when a global
        ``ABS_KEY`` is set. Adds one per active user whose ABS client is
        configured with a token distinct from the global one.
        """
        global_server = os.environ.get("ABS_SERVER", "")
        global_token = os.environ.get("ABS_KEY", "")

        targets: list[tuple] = []
        seen_tokens: set[str] = set()

        if global_token:
            targets.append((None, global_server, global_token))
            seen_tokens.add(global_token)

        registry = self._registry
        if registry is None or not hasattr(self._db, "list_users"):
            return targets

        try:
            users = [u for u in self._db.list_users() if getattr(u, "active", 1)]
        except Exception as e:
            logger.warning("ABS Socket.IO: could not list users for per-user listeners: %s", e)
            return targets

        for user in users:
            try:
                bundle = registry.get_clients(user.id)
            except Exception as e:
                logger.warning(
                    "ABS Socket.IO: skipping user %s (client build failed): %s",
                    getattr(user, "id", None), e,
                )
                continue

            abs_sync = (getattr(bundle, "sync_clients", None) or {}).get("ABS")
            abs_client = getattr(abs_sync, "abs_client", None)
            if not abs_client or not abs_client.is_configured():
                continue

            token = resolve_setting(bundle.credentials, "ABS_KEY")
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            server = resolve_setting(bundle.credentials, "ABS_SERVER", global_server)
            targets.append((user.id, server, token))

        return targets

    def start(self) -> None:
        """Build and start a listener thread for every target."""
        targets = self._listener_targets()
        if not targets:
            logger.warning(
                "ABS Socket.IO: no configured ABS token found — no listeners started"
            )
            return

        for user_id, server, token in targets:
            listener = ABSSocketListener(
                abs_server_url=server,
                abs_api_token=token,
                database_service=self._db,
                sync_manager=self._sync_manager,
                user_id=user_id,
            )
            self._listeners.append(listener)
            thread = threading.Thread(target=listener.start, daemon=True)
            thread.start()
            self._threads.append(thread)

        scopes = ["global" if uid is None else f"user {uid}" for uid, _, _ in targets]
        logger.info(
            "🔌 ABS Socket.IO: started %d listener(s) — %s",
            len(targets), ", ".join(scopes),
        )

    def stop(self) -> None:
        """Disconnect all listeners."""
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception as e:
                logger.debug("ABS Socket.IO: error stopping listener: %s", e)
