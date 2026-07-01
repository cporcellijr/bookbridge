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

logger = logging.getLogger(__name__)


class ClientPoller:
    """Background service that polls configured clients at per-client intervals."""

    # Keys match the container.sync_clients dict
    _POLLABLE = [
        ('Storyteller', 'STORYTELLER'),
        ('BookLore', 'BOOKLORE'),
        ('BookLoreAudio', 'BOOKLORE_AUDIO'),
        ('BookOrbit', 'BOOKORBIT'),
        ('BookOrbitAudio', 'BOOKORBIT_AUDIO'),
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
        self._last_known: dict[tuple, float] = {}  # {(client_name, abs_id): last_pct}
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
            watch_svc = self._shelf_watch_services.get(client_name)
            if watch_svc:
                try:
                    watch_svc.process_watch_shelf()
                except Exception as e:
                    logger.debug(f"ClientPoller: shelf-watch run failed: {e}")
            self._poll_client(client_name)

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

        recent = get_recent_write(client_name, abs_id, user_id=user_id)
        if recent is None and user_id is not None:
            recent = get_recent_write(client_name, abs_id, user_id=None)
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
                last_pct = self._last_known.get(cache_key)

                if last_pct is None:
                    logger.debug(
                        f"📡 {client_name} poll: '{book.abs_title}' initial position cached ({current_pct:.1%})"
                    )
                elif abs(current_pct - last_pct) > 0.001:
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

                self._last_known[cache_key] = current_pct

            except Exception as e:
                logger.debug(f"ClientPoller: poll check failed for {client_name}/{getattr(book, 'abs_title', '?')}: {e}")

        return checked
