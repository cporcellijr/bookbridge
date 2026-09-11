"""
Per-client polling service — lightweight background poller that checks configured
clients for progress changes without running the full sync pipeline.

When a position change is detected, it triggers sync_manager.sync_cycle() for that
book only. Clients in 'global' poll mode are excluded — they are covered by the
normal global sync cycle.
"""

import logging
import os
import threading
import time
from collections.abc import Mapping, Sequence

from src.services import observation_trail

logger = logging.getLogger(__name__)


class ClientPoller:
    """Background service that polls configured clients at per-client intervals."""

    # Keys match the container.sync_clients dict
    _POLLABLE = [
        ('Storyteller', 'STORYTELLER'),
        ('BookLore', 'BOOKLORE'),
        ('BookFusion', 'BOOKFUSION'),
        ('BookLoreAudio', 'BOOKLORE_AUDIO'),
        ('BookOrbit', 'BOOKORBIT'),
        ('BookOrbitAudio', 'BOOKORBIT_AUDIO'),
        ('Kavita', 'KAVITA'),
        ('CWA', 'CWA_SYNC'),
    ]

    def __init__(self, database_service, sync_manager, sync_clients_dict: dict,
                 shelf_watch_service=None, shelf_watch_services: dict = None,
                 user_client_registry=None):
        self._db = database_service
        self._sync_manager = sync_manager
        self._sync_clients = sync_clients_dict
        # Multi-user: when present, poll each user's own clients and trigger
        # their sync_cycle. When absent, fall back to the global client dict.
        self._registry = user_client_registry
        self._shelf_watch_service = shelf_watch_service
        # Map poll-client name -> shelf-watch service so each source's watch shelf
        # fires on that source's custom poll tick. Falls back to the legacy single
        # service mapped to 'BookLore'.
        if shelf_watch_services:
            self._shelf_watch_services = dict(shelf_watch_services)
        elif shelf_watch_service:
            self._shelf_watch_services = {'BookLore': shelf_watch_service}
        else:
            self._shelf_watch_services = {}
        self._last_known: dict[tuple, object] = {}  # {(user_id, client_name, abs_id): state fingerprint}
        self._last_poll: dict[str, float] = {}     # {client_name: last_poll_timestamp}
        # Deferred syncs for clients with {PREFIX}_POLL_WAIT_FOR_SETTLE enabled:
        # a detected change is held until a later poll shows no further movement
        # (the reader paused/stopped), then the sync cycle runs once.
        self._pending_sync: dict[tuple, float] = {}  # {(client_name, abs_id): pct at detection}
        self._running = False
        # Allow real user jumps through even inside self-write suppression windows.
        self._echo_tolerance = float(
            os.environ.get("CLIENT_POLLER_SELF_WRITE_ECHO_PERCENT", "1.0")
        ) / 100.0

    _STATE_FINGERPRINT_KEYS = (
        "ts",
        "service_updated_at",
        "href",
        "frag",
        "fragment",
        "fragments",
        "chapter_progress",
        "css_selector",
        "position",
        "match_index",
        "cfi",
    )

    @classmethod
    def _freeze_state_value(cls, value):
        if isinstance(value, Mapping):
            return tuple(sorted((str(k), cls._freeze_state_value(v)) for k, v in value.items()))
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(cls._freeze_state_value(v) for v in value)
        return value

    @classmethod
    def _state_fingerprint(cls, current: dict) -> tuple:
        """Small stable snapshot used to detect position changes during polling.

        Storyteller can advance a locator/timestamp without changing the rounded
        book percentage enough to cross the old poll threshold. Manual sync sees
        those richer fields; the poller must watch them too.
        """
        if not isinstance(current, dict):
            return ()
        details = tuple(
            (key, cls._freeze_state_value(current.get(key)))
            for key in cls._STATE_FINGERPRINT_KEYS
            if current.get(key) is not None
        )
        return ("state", current.get("pct"), details)

    @staticmethod
    def _cached_pct(cached, fallback=None):
        if isinstance(cached, tuple):
            if len(cached) == 3 and cached[0] == "state":
                return cached[1]
            for key, value in cached:
                if key == "pct":
                    return value
            return fallback
        return cached if cached is not None else fallback

    @staticmethod
    def _state_changed(last_marker, current_marker, last_pct, current_pct, pct_threshold=0.001) -> bool:
        if not isinstance(last_marker, tuple):
            return abs(current_pct - last_pct) > pct_threshold
        if abs(current_pct - last_pct) > pct_threshold:
            return True
        if (
            len(current_marker) == 3
            and len(last_marker) == 3
            and current_marker[0] == last_marker[0] == "state"
        ):
            return current_marker[2] != last_marker[2]
        return current_marker != last_marker

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the polling loop. Call from a daemon thread."""
        self._running = True
        logger.info(f"📡 Per-client poller started ({self._format_config_summary()})")
        while self._running:
            try:
                self._poll_cycle()
            except Exception as e:
                logger.debug(f"ClientPoller: cycle error: {e}")
            time.sleep(10)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _format_config_summary(self) -> str:
        parts = []
        for client_name, env_prefix in self._POLLABLE:
            mode = os.environ.get(f'{env_prefix}_POLL_MODE', 'global').lower()
            if mode == 'custom':
                interval = self._get_interval(env_prefix)
                parts.append(f"{client_name}: {interval}s")
            else:
                parts.append(f"{client_name}: global")
        return ', '.join(parts) if parts else 'none'

    def _get_interval(self, env_prefix: str, default: int = 300) -> int:
        try:
            return int(os.environ.get(f'{env_prefix}_POLL_SECONDS', str(default)))
        except ValueError:
            return default

    def _is_settle_wait_enabled(self, client_name: str) -> bool:
        env_prefix = dict(self._POLLABLE).get(client_name)
        if not env_prefix:
            return False
        raw = os.environ.get(f'{env_prefix}_POLL_WAIT_FOR_SETTLE', 'false')
        return str(raw).strip().lower() in ('true', '1', 'yes', 'on')

    def _trigger_or_defer_sync(self, client_name: str, book, last_pct: float, current_pct: float,
                               wait_for_settle: bool, during_suppression: bool = False,
                               user_id=None) -> None:
        """Run the sync cycle for a detected change, or hold it until the
        position stops moving when settle-wait is enabled for this client."""
        jump_note = (
            " during suppression window; treating as external jump"
            if during_suppression else ""
        )
        # Both callers of this method have already ruled out our own write-back, so
        # everything reaching here is movement the user actually made. Recording it
        # is instrumentation only -- nothing reads the trail to make a decision yet
        # (issue #215 phase 0).
        try:
            observation_trail.record_observation(
                client_name, book.abs_id, current_pct, source="poll", user_id=user_id,
            )
        except Exception as trail_err:
            logger.debug(f"Could not record observation for '{client_name}': {trail_err}", exc_info=True)
        if wait_for_settle:
            self._pending_sync[(user_id, client_name, book.abs_id)] = current_pct
            logger.info(
                f"📡 {client_name} poll: '{book.abs_title}' moved "
                f"{last_pct:.1%} → {current_pct:.1%}{jump_note}; waiting for position to settle"
            )
            return
        logger.info(
            f"📡 {client_name} poll: '{book.abs_title}' moved "
            f"{last_pct:.1%} → {current_pct:.1%}{jump_note}"
            f"{' and' if during_suppression else ' —'} triggering sync"
        )
        threading.Thread(
            target=self._sync_manager.sync_cycle,
            kwargs={'target_abs_id': book.abs_id, 'user_id': user_id},
            daemon=True,
        ).start()

    def _poll_targets(self, client_name: str):
        """Return [(user_id, sync_client)] to poll for this client.

        Multi-user: each active user's own configured client. Falls back to the
        global client (user_id=None) when no registry/users are available."""
        registry = self._registry
        if registry is None or not hasattr(self._db, 'list_users'):
            client = self._sync_clients.get(client_name)
            return [(None, client)] if client else []
        try:
            users = [u for u in self._db.list_users() if getattr(u, 'active', 1)]
        except Exception as e:
            logger.debug(f"ClientPoller: could not list users: {e}")
            users = []
        if not users:
            client = self._sync_clients.get(client_name)
            return [(None, client)] if client else []
        targets = []
        for user in users:
            try:
                bundle = registry.get_clients(user.id)
                client = bundle.sync_clients.get(client_name)
                if client and client.is_configured():
                    targets.append((user.id, client))
            except Exception as e:
                logger.debug(f"ClientPoller: user {getattr(user,'id',None)} bundle failed: {e}")
        return targets

    def _poll_cycle(self) -> None:
        """Check each configured client if it is due for a poll."""
        now = time.time()
        for client_name, env_prefix in self._POLLABLE:
            mode = os.environ.get(f'{env_prefix}_POLL_MODE', 'global').lower()
            if mode != 'custom':
                continue

            interval = self._get_interval(env_prefix)
            last = self._last_poll.get(client_name, 0)
            if now - last < interval:
                continue

            self._last_poll[client_name] = now
            # "Up Next" shelf-watch runs on the same cadence as its source's poll
            # when {SOURCE}_POLL_MODE=custom. In global mode the check is invoked
            # from sync_manager._sync_cycle_internal instead.
            # Per-user: run shelf-watch once per active user so each user's
            # shelves, clients, and BookOrbit links are processed independently.
            watch_svc = self._shelf_watch_services.get(client_name)
            if watch_svc:
                user_targets = []
                if self._registry and hasattr(self._db, 'list_users'):
                    try:
                        user_targets = [u.id for u in self._db.list_users()
                                        if getattr(u, 'active', 1)]
                    except Exception:
                        user_targets = []
                if not user_targets:
                    user_targets = [None]
                for uid in user_targets:
                    self._run_shelf_watch_for_user(watch_svc, client_name, uid)
            self._poll_client(client_name)

    def _run_shelf_watch_for_user(self, watch_svc, client_name, user_id):
        """Run shelf-watch for one user with that user's context bound.

        Binds the user's client bundle, user_id, and per-user credentials so
        that SuggestionsService helpers (``uc()``, ``user_setting()``) resolve
        the same user's library and settings.
        """
        from src.utils.user_context import (
            set_current_user_id, reset_current_user_id,
            set_current_user_credentials, reset_current_user_credentials,
        )

        uid_token = None
        creds_token = None
        try:
            if user_id is not None and self._registry is not None:
                try:
                    bundle = self._registry.get_clients(user_id)
                except Exception:
                    bundle = None

                try:
                    uid_token = set_current_user_id(user_id)
                except Exception:
                    pass

                try:
                    creds = self._db.get_user_credentials(user_id) or {}
                    from src.utils.user_config import (
                        _ALLOW_GLOBAL_FALLBACK_KEY,
                        PER_USER_CREDENTIAL_KEYS,
                        global_fallback_allowed,
                    )
                    user = self._db.get_user(user_id) if hasattr(self._db, 'get_user') else None
                    creds = {k: v for k, v in creds.items() if k in PER_USER_CREDENTIAL_KEYS}
                    creds[_ALLOW_GLOBAL_FALLBACK_KEY] = global_fallback_allowed(self._db, user)
                    creds_token = set_current_user_credentials(creds)
                except Exception:
                    pass

            watch_svc.process_watch_shelf(user_id=user_id)
        except Exception as e:
            logger.debug(f"ClientPoller: shelf-watch run failed for user {user_id}: {e}")
        finally:
            if creds_token is not None:
                reset_current_user_credentials(creds_token)
            if uid_token is not None:
                reset_current_user_id(uid_token)

    def _poll_client(self, client_name: str) -> None:
        """Poll this client for every target user (or globally) and trigger
        per-user sync on change."""
        targets = self._poll_targets(client_name)
        if not targets:
            return

        try:
            active_books = self._db.get_books_by_status('active')
        except Exception as e:
            logger.debug(f"ClientPoller: could not fetch active books: {e}")
            return

        wait_for_settle = self._is_settle_wait_enabled(client_name)

        total_checked = 0
        for user_id, sync_client in targets:
            total_checked += self._poll_client_for_user(
                client_name, sync_client, user_id, active_books, wait_for_settle
            )

        logger.debug(
            f"📡 {client_name} poll: checked {total_checked} across {len(targets)} target(s)"
        )

    def _self_write_window(self, client_name: str) -> int:
        """Self-write lookback covering one full poll gap for this client.

        A push is only observable at the NEXT poll of this client — up to a
        whole poll interval later, far past the tracker's 60s default. With the
        short window, the echo of our own write (fresh timestamp/locator, same
        percentage) reads as an external change and bounces a spurious sync
        cycle after every push. Real jumps still get through the widened
        window via the echo-percentage tolerance check.
        """
        env_prefix = dict(self._POLLABLE).get(client_name)
        interval = self._get_interval(env_prefix) if env_prefix else 0
        return max(interval + 60, 60)

    def _recent_self_write(self, client_name: str, abs_id: str, user_id):
        """Return BookBridge's recent write for this client/book, if any.

        Consults the polled user's namespace and — for a per-user poll — the
        global (``user_id=None``) namespace, because a sync triggered by the
        global ABS socket listener (the admin, whose token equals ``ABS_KEY``)
        runs unscoped and records its pushes under ``None``. Without the fallback
        a per-user poll mistakes that push for an external change and bounces a
        sync straight back — an instant-sync feedback loop.
        """
        from src.services.write_tracker import get_recent_write

        window = self._self_write_window(client_name)
        recent = get_recent_write(client_name, abs_id, suppression_window=window, user_id=user_id)
        if recent is None and user_id is not None:
            recent = get_recent_write(client_name, abs_id, suppression_window=window, user_id=None)
        return recent

    def _poll_client_for_user(self, client_name, sync_client, user_id, active_books, wait_for_settle) -> int:
        """Fetch current position for each active book for one user and trigger
        sync on change. Returns the number of books checked."""
        if not sync_client or not sync_client.is_configured():
            return 0

        # Per-user poll: restrict to the books this user actually claimed. The
        # catalog is shared, so scanning every active book wastes a network call
        # per book and — where two users share a client (same KoSync account) —
        # lets a delta be attributed to whichever bundle observed it first. A
        # global/None poll (single-user) keeps scanning everything.
        linked_ids = None
        if user_id is not None:
            try:
                linked_ids = self._db.get_linked_abs_ids(user_id)
            except Exception as e:
                logger.debug(f"ClientPoller: could not resolve linked books for user {user_id}: {e}")
                linked_ids = None

        checked = 0
        for book in active_books:
            try:
                if linked_ids is not None and book.abs_id not in linked_ids:
                    continue
                if hasattr(sync_client, "supports_book") and not sync_client.supports_book(book):
                    continue
                current_state = sync_client.get_service_state(book, prev_state=None)
                if current_state is None:
                    continue

                current_pct = current_state.current.get('pct')
                if current_pct is None:
                    continue

                checked += 1
                cache_key = (user_id, client_name, book.abs_id)
                current_marker = self._state_fingerprint(current_state.current)
                last_marker = self._last_known.get(cache_key)
                last_pct = self._cached_pct(last_marker, fallback=current_pct)
                marker_changed = self._state_changed(last_marker, current_marker, last_pct, current_pct)

                if last_marker is None:
                    logger.debug(
                        f"📡 {client_name} poll: '{book.abs_title}' initial position cached ({current_pct:.1%})"
                    )
                elif marker_changed:
                    # Check write-suppression before acting.
                    recent = self._recent_self_write(client_name, book.abs_id, user_id)
                    if recent is not None:
                        recent_pct = recent.get("pct")
                        if (
                            recent_pct is not None
                            and abs(current_pct - recent_pct) > self._echo_tolerance
                        ):
                            self._trigger_or_defer_sync(
                                client_name, book, last_pct, current_pct,
                                wait_for_settle, during_suppression=True, user_id=user_id,
                            )
                        else:
                            logger.debug(
                                f"📡 {client_name} poll: Ignoring self-triggered change for '{book.abs_title}'"
                            )
                    else:
                        self._trigger_or_defer_sync(
                            client_name, book, last_pct, current_pct, wait_for_settle, user_id=user_id
                        )
                elif self._pending_sync.pop((user_id, client_name, book.abs_id), None) is not None:
                    # A deferred change has settled. Re-check write-suppression: a
                    # settle wait spans two poll intervals and can outlast the 60s
                    # suppression window, so a position that is still just an echo of
                    # our own push must not bounce a sync back.
                    recent = self._recent_self_write(client_name, book.abs_id, user_id)
                    recent_pct = recent.get("pct") if recent else None
                    still_self_echo = recent is not None and (
                        recent_pct is None
                        or abs(current_pct - recent_pct) <= self._echo_tolerance
                    )
                    if still_self_echo:
                        logger.debug(
                            f"📡 {client_name} poll: '{book.abs_title}' settled at "
                            f"{current_pct:.1%} but still self-write — skipping"
                        )
                    else:
                        logger.info(
                            f"📡 {client_name} poll: '{book.abs_title}' position settled at "
                            f"{current_pct:.1%} — triggering sync"
                        )
                        threading.Thread(
                            target=self._sync_manager.sync_cycle,
                            kwargs={'target_abs_id': book.abs_id, 'user_id': user_id},
                            daemon=True,
                        ).start()

                self._last_known[cache_key] = current_marker

            except Exception as e:
                logger.debug(f"ClientPoller: poll check failed for {client_name}/{getattr(book, 'abs_title', '?')}: {e}")

        return checked
