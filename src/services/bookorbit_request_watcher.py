"""Watch BookOrbit's request channel so a finished request is paired within seconds.

BookOrbit broadcasts an empty ``book-requests:changed`` on its ``/book-requests``
Socket.IO namespace whenever any request changes, including a download becoming
available with its ``matchedBookId``. Each active reader with BookOrbit on keeps
one connection under their own login; a change, or a (re)connect that may have
missed one, re-checks that reader's open requests by id. The regular sync cycle
still checks requests, so a dropped connection only costs speed.
"""
import logging
import threading
import time
from typing import Any, Callable, Optional

import socketio

from src.utils.logging_utils import get_persistent_condition_logger

logger = logging.getLogger(__name__)

NAMESPACE = "/book-requests"
CHANGED_EVENT = "book-requests:changed"


class BookOrbitRequestWatcher:
    """One supervised request-channel connection per active reader; a change calls ``on_change(user_id)``."""

    def __init__(self, database_service: Any, user_client_registry: Any,
                 on_change: Callable[[int], None], *, debounce_secs: float = 5.0,
                 idle_secs: float = 300.0, restart_base_secs: float = 5.0,
                 restart_max_secs: float = 300.0, healthy_session_secs: float = 60.0) -> None:
        self._db = database_service
        self._registry = user_client_registry
        self._on_change = on_change
        self._debounce_secs = debounce_secs
        self._idle_secs = idle_secs
        self._restart_base_secs = restart_base_secs
        self._restart_max_secs = restart_max_secs
        self._healthy_session_secs = healthy_session_secs
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._supervisors: dict[int, threading.Thread] = {}
        self._sockets: dict[int, socketio.Client] = {}
        self._due: dict[int, float] = {}

    def start(self) -> None:
        """Start the user scan (which spawns per-user supervisors) and the debounce loop."""
        threading.Thread(target=self._debounce_loop, daemon=True, name="bookorbit-requests-debounce").start()
        threading.Thread(target=self._user_loop, daemon=True, name="bookorbit-requests-users").start()

    def stop(self) -> None:
        """Stop supervising and close every connection."""
        self._stop.set()
        with self._lock:
            sockets = list(self._sockets.values())
        for sio in sockets:
            try:
                sio.disconnect()
            except Exception as e:
                logger.debug("BookOrbit requests: error closing a connection: %s", e)

    # ---- change handling -------------------------------------------------

    def _schedule(self, user_id: int) -> None:
        """Queue a check; a burst of changes becomes one check ``debounce_secs`` after its first."""
        with self._lock:
            self._due.setdefault(user_id, time.monotonic() + self._debounce_secs)

    def _fire_due(self) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [user_id for user_id, due in self._due.items() if due <= now]
            for user_id in ready:
                del self._due[user_id]
        for user_id in ready:
            try:
                self._on_change(user_id)
            except Exception:
                logger.warning("BookOrbit requests: checking user %s's requests failed", user_id, exc_info=True)

    def _debounce_loop(self) -> None:
        # One thread runs every check, so a reader's checks never overlap each other.
        while not self._stop.wait(1.0):
            self._fire_due()

    # ---- supervision -----------------------------------------------------

    def _user_loop(self) -> None:
        """Supervise each active user, looking again for new accounts every ``idle_secs``."""
        while not self._stop.is_set():
            try:
                for user in self._db.list_users():
                    if getattr(user, "active", 1) and not self._running(user.id):
                        self._spawn(user.id)
            except Exception:
                logger.warning("BookOrbit requests: could not list users", exc_info=True)
            self._stop.wait(self._idle_secs)

    def _running(self, user_id: int) -> bool:
        with self._lock:
            thread = self._supervisors.get(user_id)
        return thread is not None and thread.is_alive()

    def _spawn(self, user_id: int) -> None:
        thread = threading.Thread(target=self._supervise, args=(user_id,), daemon=True,
                                  name=f"bookorbit-requests-{user_id}")
        with self._lock:
            self._supervisors[user_id] = thread
        thread.start()

    def _bookorbit_client(self, user_id: int) -> tuple[bool, Optional[Any]]:
        """``(account_active, configured BookOrbit client or None)`` for this user right now."""
        user = self._db.get_user(user_id)
        if user is None or not getattr(user, "active", 1):
            return False, None
        client = getattr(self._registry.get_clients(user_id), "bookorbit_client", None)
        return True, (client if client is not None and client.is_configured() else None)

    def _supervise(self, user_id: int) -> None:
        """Keep one connection open for the user, reconnecting (with backoff) with fresh credentials."""
        key = f"bookorbit_request_watch:{user_id}"
        backoff = self._restart_base_secs
        while not self._stop.is_set():
            try:
                active, client = self._bookorbit_client(user_id)
            except Exception:
                logger.warning("BookOrbit requests: could not load user %s's BookOrbit client", user_id, exc_info=True)
                active, client = True, None
            if not active:
                return
            if client is None:
                self._stop.wait(self._idle_secs)
                continue
            started = time.monotonic()
            self._watch(user_id, client, key)
            if time.monotonic() - started >= self._healthy_session_secs:
                backoff = self._restart_base_secs
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, self._restart_max_secs)

    def _watch(self, user_id: int, client: Any, key: str) -> None:
        """Hold one connection until it ends; a connect and every change schedule a check."""
        token, base_url = client._get_fresh_token(), client._get_base_url()
        if not token or not base_url:
            get_persistent_condition_logger().warn(
                logger, key, "⚠️ BookOrbit requests: user %s has no BookOrbit login to watch requests with", user_id,
            )
            return
        sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        sio.on("connect", lambda *_: self._schedule(user_id), namespace=NAMESPACE)
        sio.on(CHANGED_EVENT, lambda *_: self._schedule(user_id), namespace=NAMESPACE)
        with self._lock:
            if self._stop.is_set():
                return
            self._sockets[user_id] = sio
        try:
            sio.connect(base_url, namespaces=[NAMESPACE], auth={"token": token},
                        transports=["websocket"], wait_timeout=10)
            get_persistent_condition_logger().resolve(
                logger, key, "📡 BookOrbit requests: watching user %s's requests again", user_id,
            )
            logger.info("📡 BookOrbit requests: watching user %s's requests", user_id)
            sio.wait()
        except Exception:
            get_persistent_condition_logger().warn(
                logger, key,
                "⚠️ BookOrbit requests: could not watch user %s's requests; the regular sync still checks them",
                user_id, exc_info=True,
            )
        finally:
            with self._lock:
                if self._sockets.get(user_id) is sio:
                    del self._sockets[user_id]
            try:
                sio.disconnect()
            except Exception as e:
                logger.debug("BookOrbit requests: error closing user %s's connection: %s", user_id, e)
