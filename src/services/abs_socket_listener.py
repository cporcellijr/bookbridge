"""
ABS Socket.IO Listener — real-time progress sync via Audiobookshelf websocket.

Connects to ABS as a Socket.IO client, listens for `user_item_progress_updated`
events, and triggers instant sync with debounce to avoid hammering downstream
services during active playback.
"""

import base64
import json
import logging
import os
import threading
import time

import requests
import socketio

from src.services.write_tracker import is_own_write as _tracker_is_own_write

logger = logging.getLogger(__name__)

_DEFAULT_DEBOUNCE_SECONDS = 30
_SELF_WRITE_SLACK_SECONDS = 60

# ---------------------------------------------------------------------------
# ABS-specific write-suppression wrapper around the shared tracker.
# ---------------------------------------------------------------------------


def is_own_write(abs_id: str, suppression_window: int = 60, user_id=None) -> bool:
    """Return True if a recent ABS progress event was caused by our own write."""
    return _tracker_is_own_write('ABS', abs_id, suppression_window, user_id=user_id)


def _get_debounce_window() -> int:
    """Return the current ABS Socket.IO debounce interval in seconds."""
    try:
        value = int(
            os.environ.get(
                "ABS_SOCKET_DEBOUNCE_SECONDS",
                str(_DEFAULT_DEBOUNCE_SECONDS),
            )
        )
        return max(0, value)
    except ValueError:
        return _DEFAULT_DEBOUNCE_SECONDS


class ABSSocketListener:
    """Persistent Socket.IO connection to Audiobookshelf for real-time sync."""

    def __init__(
        self,
        abs_server_url: str,
        abs_api_token: str,
        database_service,
        sync_manager,
        user_id=None,
    ):
        self._server_url = abs_server_url.rstrip("/").replace("/api", "")
        self._api_token = abs_api_token
        self._socket_token: str | None = None
        self._db = database_service
        self._sync_manager = sync_manager
        # Multi-user: when set, this listener is authenticated as a specific
        # user's ABS account and triggers that user's scoped sync cycle. When
        # None it is the global/admin listener (single-user behavior unchanged).
        self._user_id = user_id
        self._scope_suffix = f" [user {user_id}]" if user_id is not None else ""

        # {abs_id: last_event_timestamp}
        self._pending: dict[str, float] = {}
        # Track which abs_ids already had a sync fired for the current event
        self._fired: set[str] = set()
        self._lock = threading.Lock()
        self._debounce_stop_event = threading.Event()
        self._auth_failed_event = threading.Event()
        self._auth_ok_event = threading.Event()

        self._sio = socketio.Client(
            reconnection=True,
            logger=False,
            engineio_logger=False,
        )
        self._register_handlers()

    # ------------------------------------------------------------------
    # Token acquisition
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_token(token: str) -> str:
        """Return a safe diagnostic string for a token (type + masked preview)."""
        if not token:
            return "<empty>"
        kind = "JWT" if token.startswith("eyJ") else "legacy"
        if len(token) > 12:
            preview = f"{token[:6]}...{token[-4:]}"
        else:
            preview = "***"
        return f"{kind} len={len(token)} [{preview}]"

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict | None:
        """Decode JWT payload without verification (for diagnostics only)."""
        if not token or not token.startswith("eyJ"):
            return None
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            return json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:
            return None

    def _acquire_socket_token(self) -> str | None:
        """
        Exchange the API Key for a socket-compatible user token.

        ABS v2.26.0+ API Keys (JWT with type:"api") work for REST API calls
        but are not accepted by the Socket.IO auth handler. The user's legacy
        token (stored in the user object) IS accepted.

        Returns None if the exchange fails after all retries.
        """
        logger.debug(
            f"ABS Socket.IO: API token is {self._describe_token(self._api_token)}"
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                url = f"{self._server_url}/api/me"
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self._api_token}"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    username = user_data.get("username", "unknown")
                    abs_type = user_data.get("type", "unknown")
                    legacy_token = user_data.get("token")
                    logger.debug(
                        f"ABS Socket.IO: /api/me returned user='{username}' "
                        f"type='{abs_type}' "
                        f"token={self._describe_token(legacy_token) if legacy_token else '<missing>'}"
                    )
                    if legacy_token and legacy_token != self._api_token:
                        logger.info("🔌 ABS Socket.IO: Acquired user token for socket auth")
                        # Probe /api/authorize for a fresher token; non-fatal if unavailable
                        try:
                            auth_resp = requests.post(
                                f"{self._server_url}/api/authorize",
                                headers={"Authorization": f"Bearer {self._api_token}"},
                                timeout=10,
                            )
                            if auth_resp.status_code == 200:
                                authorized_token = auth_resp.json().get("user", {}).get("token")
                                if authorized_token and authorized_token != legacy_token:
                                    logger.debug(
                                        f"ABS Socket.IO: /api/authorize returned fresher token "
                                        f"{self._describe_token(authorized_token)}"
                                    )
                                    legacy_token = authorized_token
                        except Exception as _e:
                            logger.debug(f"ABS Socket.IO: /api/authorize probe failed (non-fatal) — {_e}")
                        _payload = self._decode_jwt_payload(legacy_token)
                        if _payload:
                            logger.debug(
                                f"ABS Socket.IO: Token payload — type={_payload.get('type', '?')} "
                                f"userId={str(_payload.get('userId', '?'))[:8]} "
                                f"iat={_payload.get('iat', '?')} exp={_payload.get('exp', '?')}"
                            )
                        return legacy_token
                    logger.info("🔌 ABS Socket.IO: Using API token directly (same as user token)")
                    _payload = self._decode_jwt_payload(self._api_token)
                    if _payload:
                        logger.debug(
                            f"ABS Socket.IO: Token payload — type={_payload.get('type', '?')} "
                            f"userId={str(_payload.get('userId', '?'))[:8]} "
                            f"iat={_payload.get('iat', '?')} exp={_payload.get('exp', '?')}"
                        )
                    return self._api_token
                else:
                    logger.warning(f"⚠️ ABS Socket.IO: /api/me returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ ABS Socket.IO: Token exchange attempt {attempt}/{max_retries} failed — {e}")
            if attempt < max_retries:
                time.sleep(5 * attempt)

        logger.error("❌ ABS Socket.IO: Could not acquire socket token after retries — listener will not start")
        return None

    def _build_token_strategies(self) -> list[str]:
        """Return ordered list of tokens to try for socket auth (deduped).

        Strategy 0: user token from /api/me (preferred — type "user" accepted by all ABS versions)
        Strategy 1: API key directly (fallback if user token is rejected)
        """
        primary = self._acquire_socket_token()
        if not primary:
            return []
        strategies: list[str] = [primary]
        if self._api_token and self._api_token != primary:
            strategies.append(self._api_token)
        return strategies

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        def connect():
            logger.info(
                f"🔌 ABS Socket.IO: Connected — sending auth "
                f"({self._describe_token(self._socket_token)})"
            )
            sio.emit("auth", self._socket_token)

        @sio.event
        def disconnect():
            logger.warning("⚠️ ABS Socket.IO: Disconnected (will auto-reconnect)")

        @sio.on("init")
        def on_init(data):
            username = "unknown"
            if isinstance(data, dict):
                user = data.get("user", {})
                if isinstance(user, dict):
                    username = user.get("username", "unknown")
            logger.info(f"🔌 ABS Socket.IO: Authenticated as '{username}'")
            self._auth_ok_event.set()

        @sio.on("auth_failed")
        def on_auth_failed(*args):
            logger.warning(
                f"⚠️ ABS Socket.IO: Auth failed — token "
                f"{self._describe_token(self._socket_token)} was rejected."
            )
            self._auth_failed_event.set()
            sio.disconnect()

        @sio.on("connect_error")
        def on_connect_error(data=None):
            logger.debug("ABS Socket.IO: Connection error (auto-reconnect will handle it)")

        @sio.on("user_item_progress_updated")
        def on_progress_updated(data):
            self._handle_progress_event(data)

    def _handle_progress_event(self, data: dict) -> None:
        """Record a progress event in the debounce dict if it belongs to an active book."""
        if not isinstance(data, dict):
            return

        # ABS event structure: {id, sessionId, deviceDescription, data: {libraryItemId, ...}}
        # The `id` at top level is the mediaProgress record ID (not useful).
        # The actual book ID (`libraryItemId`) is inside the nested `data` dict.
        library_item_id = None

        # Check nested `data` dict first (modern ABS format)
        inner = data.get("data", {})
        if isinstance(inner, dict):
            library_item_id = inner.get("libraryItemId") or inner.get("mediaItemId")

        # Fallback to top-level fields (older ABS format)
        if not library_item_id:
            library_item_id = data.get("libraryItemId") or data.get("mediaItemId")

        if not library_item_id:
            logger.debug("ABS Socket.IO: Progress event missing libraryItemId — ignoring")
            return

        # Check if this is an active book in our database
        book = self._db.get_book(library_item_id)
        if not book:
            logger.debug(
                f"ABS Socket.IO: Progress event for '{library_item_id[:12]}...' "
                f"— unknown book, queuing suggestion check"
            )
            threading.Thread(
                target=self._sync_manager.queue_suggestion,
                kwargs={"abs_id": library_item_id, "user_id": self._user_id},
                daemon=True,
            ).start()
            return

        if book.status != "active":
            logger.debug(
                f"ABS Socket.IO: Progress event for '{library_item_id[:12]}...' "
                f"— not an active book, ignoring"
            )
            return

        with self._lock:
            self._pending[library_item_id] = time.time()
            self._fired.discard(library_item_id)

        logger.debug(f"ABS Socket.IO: Progress event recorded for '{book.abs_title}'")

    # ------------------------------------------------------------------
    # Debounce loop
    # ------------------------------------------------------------------

    def _debounce_loop(self) -> None:
        """Check pending events every 10s and fire sync after debounce window."""
        logger.debug("ABS Socket.IO: Debounce loop started")
        while not self._debounce_stop_event.wait(10):
            try:
                self._check_and_fire()
            except Exception as e:
                logger.debug(f"ABS Socket.IO: Debounce loop error: {e}")

    def _check_and_fire(self) -> None:
        """Fire sync for any books whose debounce window has elapsed."""
        now = time.time()
        debounce_window = _get_debounce_window()
        to_fire: list[str] = []

        with self._lock:
            for abs_id, last_event in list(self._pending.items()):
                if abs_id in self._fired:
                    continue
                if now - last_event > debounce_window:
                    to_fire.append(abs_id)

            for abs_id in to_fire:
                self._fired.add(abs_id)
                del self._pending[abs_id]

        for abs_id in to_fire:
            book = self._db.get_book(abs_id)
            title = book.abs_title if book else abs_id[:12]
            # Resolve the user(s) this event belongs to. A per-user listener
            # already knows its user. The global listener (user_id=None) must fan
            # out to EVERY user who claimed the book — a book shared by several
            # users on the global ABS token would otherwise instant-sync only the
            # single owner column, leaving the other claimants on the slow poll.
            # Each push records write-suppression under that user's namespace (the
            # same one the per-user poller reads) so our own ebook pushes don't
            # echo back as "external" changes (a feedback loop). A legacy/admin
            # book with no ownership rows stays [None] — unchanged.
            if self._user_id is not None:
                target_user_ids = [self._user_id]
            else:
                target_user_ids = self._resolve_claimant_user_ids(book)

            for target_user_id in target_user_ids:
                self_write_window = max(
                    debounce_window + _SELF_WRITE_SLACK_SECONDS,
                    _SELF_WRITE_SLACK_SECONDS,
                )
                if is_own_write(
                    abs_id,
                    suppression_window=self_write_window,
                    user_id=target_user_id,
                ):
                    logger.debug(f"ABS Socket.IO: Ignoring self-triggered event for '{title}'{self._scope_suffix}")
                    continue
                logger.info(f"⚡ Socket.IO: ABS progress changed for '{title}'{self._scope_suffix} — triggering sync")
                threading.Thread(
                    target=self._sync_manager.sync_cycle,
                    kwargs={"target_abs_id": abs_id, "user_id": target_user_id},
                    daemon=True,
                ).start()

    def _resolve_claimant_user_ids(self, book) -> list:
        """User ids to instant-sync for a global-listener event.

        Returns every user that claimed the book (the ``user_books`` link table,
        which is also the isolation gate the sync cycle enforces, so firing for a
        non-claimant is a harmless no-op). Falls back to the book's owner column,
        then to ``[None]`` for a legacy/admin book with no ownership rows —
        preserving single-user/global behavior.
        """
        abs_id = getattr(book, "abs_id", None) if book is not None else None
        ids = []
        if abs_id and hasattr(self._db, "get_book_user_ids"):
            try:
                ids = list(self._db.get_book_user_ids(abs_id) or [])
            except Exception as exc:
                logger.debug(f"ABS Socket.IO: claimant resolve failed for '{abs_id}': {exc}")
                ids = []
        if ids:
            return ids
        owner = getattr(book, "user_id", None) if book is not None else None
        return [owner]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and block. Call from a daemon thread."""
        logger.info(f"🔌 ABS Socket.IO: Connecting to {self._server_url}{self._scope_suffix}")

        strategies = self._build_token_strategies()
        if not strategies:
            logger.error("❌ ABS Socket.IO: No valid token — listener will not start")
            return

        # Debounce loop does not reference self._sio; safe to start once before strategy loop.
        self._sio.start_background_task(self._debounce_loop)

        for strategy_idx, token in enumerate(strategies):
            self._socket_token = token
            self._auth_failed_event.clear()
            self._auth_ok_event.clear()

            logger.info(
                f"🔌 ABS Socket.IO: Attempting connection "
                f"(strategy {strategy_idx + 1}/{len(strategies)}, "
                f"token {self._describe_token(token)})"
            )
            try:
                self._sio.connect(
                    self._server_url,
                    transports=["websocket"],
                    auth={"token": token},
                )
                self._sio.wait()
            except Exception as e:
                logger.error(f"❌ ABS Socket.IO: Connection error — {e}")
                break

            if self._auth_ok_event.is_set():
                logger.info("ABS Socket.IO: Session ended after successful auth")
                break
            elif self._auth_failed_event.is_set():
                remaining = len(strategies) - strategy_idx - 1
                if remaining > 0:
                    logger.warning(
                        f"⚠️ ABS Socket.IO: Strategy {strategy_idx + 1} failed — "
                        f"trying {remaining} more strategy(s)"
                    )
                    # Reconstruct client to avoid reconnection state-machine races across auth retries.
                    self._sio = socketio.Client(
                        reconnection=True,
                        logger=False,
                        engineio_logger=False,
                    )
                    self._register_handlers()
                    continue
                else:
                    logger.error(
                        "❌ ABS Socket.IO: All auth strategies exhausted — "
                        "real-time sync disabled (falling back to standard polling). "
                        "To fix: check your ABS_KEY or restart ABS."
                    )
            break

    def stop(self) -> None:
        """Disconnect cleanly."""
        self._debounce_stop_event.set()
        try:
            if self._sio.connected:
                self._sio.disconnect()
                logger.info("🔌 ABS Socket.IO: Disconnected")
        except Exception as e:
            logger.debug(f"ABS Socket.IO: Error during disconnect: {e}")
