"""ABS Socket.IO manager — supervises one listener per user (multi-user).

Audiobookshelf emits ``user_item_progress_updated`` only over the socket of the
user whose progress changed, so a single listener (authenticated as the admin)
never sees other users' playback. This manager starts one
:class:`ABSSocketListener` per active user that has their own ABS token, each
authenticated as that user and triggering that user's scoped sync cycle.

The global/admin listener (``user_id=None``) is started when a global
``ABS_KEY`` is configured AND Audiobookshelf is enabled for the primary admin.
Per-user listeners are only added for users whose resolved ABS token differs from the
global one, so an admin (whose token falls back to the global key) is not
double-listened.
"""

import logging
import os
import threading
import time

from src.services.abs_socket_listener import ABSSocketListener
from src.utils.config_loader import env_truthy
from src.utils.logging_utils import get_persistent_condition_logger
from src.utils.user_config import resolve_setting

logger = logging.getLogger(__name__)


class ABSSocketManager:
    """Starts and supervises per-user ABS Socket.IO listeners."""

    def __init__(self, database_service, sync_manager, user_client_registry=None):
        self._db = database_service
        self._sync_manager = sync_manager
        self._registry = user_client_registry
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # scope label -> the listener currently running for it (for clean stop()).
        self._current_listeners: dict = {}
        # Scopes of deleted accounts: their listeners must never reconnect.
        self._retired_scopes: set[str] = set()
        # Restart/backoff tuning (instance attrs so tests can zero them out).
        self._restart_base_secs = 5.0
        self._restart_max_secs = 60.0
        self._healthy_session_secs = 60.0

    def _is_scope_enabled(self, user_id) -> bool:
        """Return whether the ABS Socket.IO listener for the given scope should run.

        For a real user_id: resolves that user's client bundle from the registry
        and returns whether its ABS client reports is_configured() — the exact
        same gate the per-user branch in _listener_targets() already applies.

        For None (global): finds the PRIMARY admin among active users, resolves
        that admin's bundle, and returns their ABS client's is_configured().

        FAILS OPEN in every uncertain case (no registry, no users listed, no
        primary admin found, database service lacking methods, or ANY exception)
        by falling back to the global ABS_ENABLED read through
        config_loader.env_truthy("ABS_ENABLED", "true"). This is a "don't do
        useless work" guard, never a security or correctness gate: wrongly
        starting a listener costs a little idle work, while wrongly refusing to
        start one silently kills real-time sync.

        Note: This deliberately only skips the ONE global listener whose token
        belongs to the primary admin. Per the comment on PER_USER_CREDENTIAL_KEYS
        in user_config.py, ABS_ENABLED is NOT in ENGINE_MIRROR_KEYS: the primary
        admin switching their own ABS off must not take the global singletons down
        for everyone else. We are skipping one listener — we are not disabling any
        client, singleton, or another user's listener.
        """
        # Fail open: if we can't determine, assume enabled
        if self._registry is None:
            return env_truthy("ABS_ENABLED", "true")

        if user_id is not None:
            # Per-user scope: check that specific user's ABS client
            try:
                bundle = self._registry.get_clients(user_id)
            except Exception as e:
                logger.debug(
                    "ABS Socket.IO: scope check failed for user %s (registry error), failing open: %s",
                    user_id, e, exc_info=True,
                )
                return env_truthy("ABS_ENABLED", "true")

            abs_sync = (getattr(bundle, "sync_clients", None) or {}).get("ABS")
            abs_client = getattr(abs_sync, "abs_client", None)
            if abs_client and hasattr(abs_client, "is_configured"):
                return bool(abs_client.is_configured())
            # If we can't determine, fail open
            return env_truthy("ABS_ENABLED", "true")

        # Global scope (user_id is None): find the primary admin and check their ABS
        if not hasattr(self._db, "list_users") or not hasattr(self._db, "is_primary_admin"):
            return env_truthy("ABS_ENABLED", "true")

        try:
            users = [u for u in self._db.list_users() if getattr(u, "active", 1)]
        except Exception as e:
            logger.debug(
                "ABS Socket.IO: global scope check failed (list_users error), failing open: %s",
                e, exc_info=True,
            )
            return env_truthy("ABS_ENABLED", "true")

        primary_admin = None
        for user in users:
            try:
                if self._db.is_primary_admin(user.id):
                    primary_admin = user
                    break
            except Exception as e:
                logger.debug(
                    "ABS Socket.IO: global scope check failed (is_primary_admin error), failing open: %s",
                    e, exc_info=True,
                )
                return env_truthy("ABS_ENABLED", "true")

        if primary_admin is None:
            logger.debug(
                "ABS Socket.IO: no primary admin found for global scope check, failing open",
            )
            return env_truthy("ABS_ENABLED", "true")

        try:
            bundle = self._registry.get_clients(primary_admin.id)
        except Exception as e:
            logger.debug(
                "ABS Socket.IO: global scope check failed for primary admin %s (registry error), failing open: %s",
                primary_admin.id, e, exc_info=True,
            )
            return env_truthy("ABS_ENABLED", "true")

        abs_sync = (getattr(bundle, "sync_clients", None) or {}).get("ABS")
        abs_client = getattr(abs_sync, "abs_client", None)
        if abs_client and hasattr(abs_client, "is_configured"):
            return bool(abs_client.is_configured())

        # Fail open if we can't determine
        return env_truthy("ABS_ENABLED", "true")

    def _listener_targets(self) -> list[tuple]:
        """Return ``[(user_id, server_url, token)]`` for each listener to start.

        Always includes the global listener (``user_id=None``) when a global
        ``ABS_KEY`` is set AND ABS is enabled for the primary admin. Adds one per
        active user whose ABS client is configured with a token distinct from the
        global one.
        """
        global_server = os.environ.get("ABS_SERVER", "")
        global_token = os.environ.get("ABS_KEY", "")

        targets: list[tuple] = []
        seen_tokens: set[str] = set()

        if global_token and self._is_scope_enabled(None):
            targets.append((None, global_server, global_token))
            seen_tokens.add(global_token)
        elif global_token:
            logger.info(
                "🔌 ABS Socket.IO: global/admin ABS listener not started — Audiobookshelf is switched off for the primary admin (per-user listeners are unaffected)"
            )

        registry = self._registry
        if registry is None or not hasattr(self._db, "list_users"):
            return targets

        try:
            users = [u for u in self._db.list_users() if getattr(u, "active", 1)]
        except Exception as e:
            logger.warning("ABS Socket.IO: could not list users for per-user listeners: %s", e, exc_info=True)
            return targets

        for user in users:
            try:
                bundle = registry.get_clients(user.id)
            except Exception as e:
                logger.warning(
                    "ABS Socket.IO: skipping user %s (client build failed): %s",
                    getattr(user, "id", None), e, exc_info=True,
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
        """Start a supervised listener thread for every target."""
        targets = self._listener_targets()
        if not targets:
            # An ABS_KEY IS present when the only reason there are no targets is
            # that Audiobookshelf is switched off for every scope. Saying "no
            # configured ABS token found" there would send the reader hunting for
            # a credential that is sitting right there, so that line is reserved
            # for the case it actually describes.
            if os.environ.get("ABS_KEY"):
                logger.info(
                    "🔌 ABS Socket.IO: no listeners started — Audiobookshelf is "
                    "switched off for every configured scope"
                )
            else:
                logger.warning(
                    "ABS Socket.IO: no configured ABS token found — no listeners started"
                )
            return

        for user_id, server, token in targets:
            scope = "global" if user_id is None else f"user {user_id}"
            thread = threading.Thread(
                target=self._supervise,
                args=(user_id, server, token, scope),
                daemon=True,
                name=f"abs-socket-{scope.replace(' ', '-')}",
            )
            thread.start()
            self._threads.append(thread)

        logger.info(
            "🔌 ABS Socket.IO: started %d supervised listener(s) — %s",
            len(targets),
            ", ".join("global" if uid is None else f"user {uid}" for uid, _, _ in targets),
        )

    def _supervise(self, user_id, server, token, scope: str) -> None:
        """Run one listener and restart it (with backoff) whenever it exits.

        ``ABSSocketListener.start()`` blocks while connected and only returns when
        the socket session ends — including an uncaught engineio teardown race
        (``write_loop_task`` ``None``) that kills the transport thread and leaves
        the client dead with no reconnect. Without this loop a dead listener stays
        dead until the process restarts, silently dropping real-time ABS instant
        sync (the poll cycle still covers it, just slower). A session that lasted a
        while resets the backoff so a transient death restarts promptly, while
        rapid immediate exits (bad token, ABS down) back off up to the cap.

        At the top of each iteration we re-check whether ABS is still enabled for
        this scope. If definitively disabled, we log and return instead of
        restarting — this gives convergence when someone toggles ABS off at
        runtime, without adding a reconcile thread. Because the helper fails open,
        a transient database or registry error can never terminate a healthy
        supervision loop — only a definite "disabled" answer does.
        """
        backoff = self._restart_base_secs
        while not self._stop_event.is_set():
            # Re-check whether this scope should still run (handles runtime toggle-off)
            # Fail open on any exception from the enable check — a transient error
            # must never terminate a healthy supervision loop.
            try:
                scope_enabled = self._is_scope_enabled(user_id)
            except Exception as e:
                logger.debug(
                    "ABS Socket.IO: scope enable check failed for %s, failing open: %s",
                    scope, e, exc_info=True,
                )
                scope_enabled = True
            if not scope_enabled:
                logger.info(
                    "🔌 ABS Socket.IO: %s listener stopping — ABS switched off for this scope",
                    scope,
                )
                return

            listener = ABSSocketListener(
                abs_server_url=server,
                abs_api_token=token,
                database_service=self._db,
                sync_manager=self._sync_manager,
                user_id=user_id,
            )
            with self._lock:
                if self._stop_event.is_set() or scope in self._retired_scopes:
                    break
                self._current_listeners[scope] = listener

            t0 = time.monotonic()
            try:
                listener.start()  # blocks until the socket session ends
            except Exception as e:
                logger.warning("🔌 ABS Socket.IO: %s listener crashed: %s", scope, e, exc_info=True)
            finally:
                listener.stop()

            if self._stop_event.is_set() or scope in self._retired_scopes:
                break

            elapsed = time.monotonic() - t0
            if elapsed >= self._healthy_session_secs:
                backoff = self._restart_base_secs
            get_persistent_condition_logger().warn(
                logger,
                f"abs_socket:{user_id}",
                "🔌 ABS Socket.IO: %s listener exited after %.0fs — restarting in %.0fs",
                scope, elapsed, backoff,
            )
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, self._restart_max_secs)

    def stop_user(self, user_id: int) -> None:
        """Disconnect a deleted account's listener and keep it from restarting."""
        scope = f"user {user_id}"
        with self._lock:
            self._retired_scopes.add(scope)
            listener = self._current_listeners.pop(scope, None)
        if listener is None:
            return
        logger.info("🔌 ABS Socket.IO: %s listener stopping — account deleted", scope)
        try:
            listener.stop()
        except Exception as e:
            logger.debug("ABS Socket.IO: error stopping listener: %s", e)

    def stop(self) -> None:
        """Stop supervising and disconnect all listeners."""
        self._stop_event.set()
        with self._lock:
            listeners = list(self._current_listeners.values())
        for listener in listeners:
            try:
                listener.stop()
            except Exception as e:
                logger.debug("ABS Socket.IO: error stopping listener: %s", e)
