import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


logger = logging.getLogger(__name__)


@dataclass
class SyncRequest:
    book_id: str
    trigger_source: str = "manual"
    trigger_service: Optional[str] = None
    requested_at: float = field(default_factory=time.time)
    reason: Optional[str] = None
    force_reconcile: bool = False


class SyncDispatcher:
    """
    Serialize sync work per book while allowing repeated triggers to coalesce.

    One book may only have one active worker at a time. If a second request
    arrives while that worker is running, the new request is merged into a
    pending rerun instead of being dropped.
    """

    def __init__(self, run_sync: Callable[[SyncRequest], None]):
        self._run_sync = run_sync
        self._lock = threading.Lock()
        self._book_state: dict[str, dict] = {}

    @staticmethod
    def _merge_requests(current: Optional[SyncRequest], new_request: SyncRequest) -> SyncRequest:
        if current is None:
            return new_request

        reasons = []
        for reason in (current.reason, new_request.reason):
            if reason and reason not in reasons:
                reasons.append(reason)

        trigger_source = new_request.trigger_source or current.trigger_source
        trigger_service = new_request.trigger_service or current.trigger_service

        return SyncRequest(
            book_id=new_request.book_id,
            trigger_source=trigger_source,
            trigger_service=trigger_service,
            requested_at=max(current.requested_at, new_request.requested_at),
            reason=" | ".join(reasons) if reasons else None,
            force_reconcile=current.force_reconcile or new_request.force_reconcile,
        )

    def request_sync(
        self,
        book_id: str,
        *,
        trigger_source: str = "manual",
        trigger_service: Optional[str] = None,
        reason: Optional[str] = None,
        force_reconcile: bool = False,
    ) -> None:
        request = SyncRequest(
            book_id=book_id,
            trigger_source=trigger_source,
            trigger_service=trigger_service,
            reason=reason,
            force_reconcile=force_reconcile,
        )

        with self._lock:
            state = self._book_state.get(book_id)
            if state and state.get("running"):
                state["pending"] = self._merge_requests(state.get("pending"), request)
                logger.info(
                    "sync_coalesced book=%s trigger_source=%s trigger_service=%s reason=%s",
                    book_id,
                    request.trigger_source,
                    request.trigger_service or "",
                    request.reason or "",
                )
                return

            self._book_state[book_id] = {"running": True, "pending": None}

        logger.info(
            "sync_requested book=%s trigger_source=%s trigger_service=%s reason=%s",
            book_id,
            request.trigger_source,
            request.trigger_service or "",
            request.reason or "",
        )
        threading.Thread(target=self._worker, args=(request,), daemon=True).start()

    def is_running(self, book_id: str) -> bool:
        with self._lock:
            return bool(self._book_state.get(book_id, {}).get("running"))

    def _worker(self, request: SyncRequest) -> None:
        current = request

        while current is not None:
            try:
                self._run_sync(current)
                logger.info(
                    "sync_completed book=%s trigger_source=%s trigger_service=%s reason=%s",
                    current.book_id,
                    current.trigger_source,
                    current.trigger_service or "",
                    current.reason or "",
                )
            except Exception:
                logger.exception(
                    "sync_failed book=%s trigger_source=%s trigger_service=%s reason=%s",
                    current.book_id,
                    current.trigger_source,
                    current.trigger_service or "",
                    current.reason or "",
                )

            with self._lock:
                state = self._book_state.get(current.book_id)
                pending = state.get("pending") if state else None
                if not pending:
                    self._book_state.pop(current.book_id, None)
                    current = None
                    continue

                state["pending"] = None
                current = pending

            logger.info(
                "sync_rerun book=%s trigger_source=%s trigger_service=%s reason=%s",
                current.book_id,
                current.trigger_source,
                current.trigger_service or "",
                current.reason or "",
            )
