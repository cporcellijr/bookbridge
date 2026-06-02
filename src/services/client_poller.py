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
                 shelf_watch_service=None):
        self._db = database_service
        self._sync_manager = sync_manager
        self._sync_clients = sync_clients_dict
        self._shelf_watch_service = shelf_watch_service
        self._last_known: dict[tuple, float] = {}  # {(client_name, abs_id): last_pct}
        self._last_poll: dict[str, float] = {}     # {client_name: last_poll_timestamp}
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
            # Grimmory "Up Next" shelf-watch runs on the same cadence as the
            # Booklore poll when BOOKLORE_POLL_MODE=custom. In global mode the
            # check is invoked from sync_manager._sync_cycle_internal instead.
            if client_name == 'BookLore' and self._shelf_watch_service:
                try:
                    self._shelf_watch_service.process_watch_shelf()
                except Exception as e:
                    logger.debug(f"ClientPoller: shelf-watch run failed: {e}")
            self._poll_client(client_name)

    def _poll_client(self, client_name: str) -> None:
        """Fetch current position for each active book and trigger sync on change."""
        from src.services.write_tracker import get_recent_write, is_own_write

        sync_client = self._sync_clients.get(client_name)
        if not sync_client or not sync_client.is_configured():
            return

        try:
            active_books = self._db.get_books_by_status('active')
        except Exception as e:
            logger.debug(f"ClientPoller: could not fetch active books: {e}")
            return

        checked = 0
        for book in active_books:
            try:
                if hasattr(sync_client, "supports_book") and not sync_client.supports_book(book):
                    continue
                current_state = sync_client.get_service_state(book, prev_state=None)
                if current_state is None:
                    continue

                current_pct = current_state.current.get('pct')
                if current_pct is None:
                    continue

                checked += 1
                cache_key = (client_name, book.abs_id)
                last_pct = self._last_known.get(cache_key)

                if last_pct is None:
                    logger.debug(
                        f"📡 {client_name} poll: '{book.abs_title}' initial position cached ({current_pct:.1%})"
                    )
                elif abs(current_pct - last_pct) > 0.001:
                    # Check write-suppression before acting
                    if is_own_write(client_name, book.abs_id):
                        recent = get_recent_write(client_name, book.abs_id)
                        recent_pct = recent.get("pct") if recent else None
                        if (
                            recent_pct is not None
                            and abs(current_pct - recent_pct) > self._echo_tolerance
                        ):
                            logger.info(
                                f"📡 {client_name} poll: '{book.abs_title}' moved "
                                f"{last_pct:.1%} → {current_pct:.1%} during suppression window; "
                                f"treating as external jump and triggering sync"
                            )
                            threading.Thread(
                                target=self._sync_manager.sync_cycle,
                                kwargs={'target_abs_id': book.abs_id},
                                daemon=True,
                            ).start()
                        else:
                            logger.debug(
                                f"📡 {client_name} poll: Ignoring self-triggered change for '{book.abs_title}'"
                            )
                    else:
                        logger.info(
                            f"📡 {client_name} poll: '{book.abs_title}' moved "
                            f"{last_pct:.1%} → {current_pct:.1%} — triggering sync"
                        )
                        threading.Thread(
                            target=self._sync_manager.sync_cycle,
                            kwargs={'target_abs_id': book.abs_id},
                            daemon=True,
                        ).start()

                self._last_known[cache_key] = current_pct

            except Exception as e:
                logger.debug(f"ClientPoller: poll check failed for {client_name}/{getattr(book, 'abs_title', '?')}: {e}")

        logger.debug(f"📡 {client_name} poll: checked {checked}/{len(active_books)} active books")
