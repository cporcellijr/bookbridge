"""
Annotation hub — BookOrbit spoke.

The bridge stores canonical KOReader-format annotations (xpointer + highlighted
text) per (user, document md5) and relays them between devices via the exchange
endpoints in kosync_server. This service extends the relay to BookOrbit's web
reader: the bridge acts as one more "device" against BookOrbit's own
koreader-plugin exchange endpoint, so BookOrbit's server does every CFI <->
xpointer conversion and the bridge never needs a render engine.

Per user with BookOrbit KOReader-sync credentials configured
(BOOKORBIT_KOSYNC_USER / BOOKORBIT_KOSYNC_KEY), each cycle:
  1. collects the documents that have local annotations plus the user's linked
     books (so web-first highlights are pulled even before any device annotates),
  2. exchanges keys + changed entries with BookOrbit (key omission propagates
     deletions in both directions),
  3. applies BookOrbit's toApply delta into the canonical store (bumping
     versions so devices pick the changes up on their next exchange), and
  4. acks what landed.

Documents BookOrbit reports as ``unmatched`` (hash unknown to it) are cached
with a recheck TTL — a book added to BookOrbit later gets picked up on the
next probe instead of staying skipped until a bridge restart.
"""

import logging
import threading
import time

from src.api.bookorbit_client import BookOrbitClient
from src.utils.user_config import resolve_setting, _ALLOW_GLOBAL_FALLBACK_KEY

logger = logging.getLogger(__name__)

SPOKE_KEY = "@bookorbit"
_MAX_BOOKS_PER_CALL = 20
_MAX_CHANGES_PER_BOOK = 50
_MAX_PULL_ROUNDS = 5
_UNMATCHED_RECHECK_SECONDS = 6 * 3600


class AnnotationSyncService:
    def __init__(self, database_service):
        self.database_service = database_service
        # {(user_id, md5): last_checked_epoch} — re-probed after the TTL.
        self._unmatched: dict[tuple[int | None, str], float] = {}
        self._lock = threading.Lock()

    def _is_unmatched(self, user_id, doc_md5: str) -> bool:
        checked_at = self._unmatched.get((user_id, doc_md5))
        if checked_at is None:
            return False
        if time.time() - checked_at >= _UNMATCHED_RECHECK_SECONDS:
            del self._unmatched[(user_id, doc_md5)]
            return False
        return True

    # ------------------------------------------------------------------
    # Cycle driver
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict:
        """Sync every configured user's annotations with BookOrbit once."""
        if not self._lock.acquire(blocking=False):
            logger.debug("Annotation sync cycle already running; skipping")
            return {"users": 0}
        try:
            users = self._enumerate_users()
            synced_users = 0
            for user_id, creds in users:
                kosync_user = str(resolve_setting(creds, "BOOKORBIT_KOSYNC_USER", "") or "").strip()
                kosync_key = BookOrbitClient.normalize_kosync_key(
                    resolve_setting(creds, "BOOKORBIT_KOSYNC_KEY", "")
                )
                if not kosync_user or not kosync_key:
                    continue
                client = BookOrbitClient(credentials=creds)
                if not str(resolve_setting(creds, "BOOKORBIT_SERVER", "") or "").strip():
                    continue
                try:
                    self.sync_user(user_id, client, kosync_user, kosync_key)
                    synced_users += 1
                except Exception as e:
                    logger.error("Annotation sync failed for user %s: %s", user_id, e, exc_info=True)
            return {"users": synced_users}
        finally:
            self._lock.release()

    def _enumerate_users(self) -> list:
        """(user_id, credential-dict) pairs; admins fall back to global settings."""
        users = []
        try:
            for user in self.database_service.list_users() or []:
                if not getattr(user, "active", 1):
                    continue
                creds = dict(self.database_service.get_user_credentials(user.id) or {})
                creds[_ALLOW_GLOBAL_FALLBACK_KEY] = bool(getattr(user, "is_admin", False))
                users.append((user.id, creds))
        except Exception as e:
            logger.debug("Annotation sync could not enumerate users: %s", e)
        return users

    # ------------------------------------------------------------------
    # Per-user exchange
    # ------------------------------------------------------------------

    def _candidate_md5s(self, user_id) -> list[str]:
        md5s = set(self.database_service.get_annotation_md5s_for_user(user_id) or [])
        # Linked/active books too, so web-first highlights get pulled.
        try:
            linked = None
            if hasattr(self.database_service, "get_linked_abs_ids"):
                linked = self.database_service.get_linked_abs_ids(user_id)
                linked = set(linked) if linked is not None else None
            for book in self.database_service.get_books_by_status("active") or []:
                if linked is not None and book.abs_id not in linked:
                    continue
                doc_hash = str(getattr(book, "kosync_doc_id", "") or "").strip().lower()
                if doc_hash and len(doc_hash) == 32:
                    md5s.add(doc_hash)
        except Exception as e:
            logger.debug("Annotation sync book enumeration failed for user %s: %s", user_id, e)
        return sorted(md5s)

    def sync_user(self, user_id, client: BookOrbitClient, kosync_user: str, kosync_key: str) -> None:
        md5s = [
            m for m in self._candidate_md5s(user_id)
            if not self._is_unmatched(user_id, m)
        ]
        if not md5s:
            return

        for start in range(0, len(md5s), _MAX_BOOKS_PER_CALL):
            chunk = md5s[start:start + _MAX_BOOKS_PER_CALL]
            self._exchange_chunk(user_id, client, kosync_user, kosync_key, chunk)

    def _exchange_chunk(self, user_id, client, kosync_user, kosync_key, md5s: list[str]) -> None:
        for _round in range(_MAX_PULL_ROUNDS):
            books_payload = []
            uploaded_ids_by_hash = {}
            tombstone_ids_by_hash = {}
            for doc_md5 in md5s:
                state = self.database_service.get_annotation_spoke_state(user_id, doc_md5, SPOKE_KEY)
                changes = state["changes"][:_MAX_CHANGES_PER_BOOK]
                uploaded_ids_by_hash[doc_md5] = [c.pop("_id") for c in changes]
                tombstone_ids_by_hash[doc_md5] = state["pending_delete_acks"]
                for change in changes:
                    # BookOrbit's DTO rejects unknown/null-required fields.
                    change.pop("serverId", None)
                    change.pop("version", None)
                    for key in list(change.keys()):
                        if change[key] is None:
                            del change[key]
                books_payload.append({
                    "hash": doc_md5,
                    "keys": state["keys"],
                    "keysComplete": True,
                    "changes": changes,
                })

            if not books_payload:
                return

            response = client.koreader_exchange_annotations(kosync_user, kosync_key, books_payload)
            if response is None:
                logger.warning("Annotation exchange with BookOrbit failed for user %s", user_id)
                return

            for unmatched_hash in response.get("unmatched") or []:
                self._unmatched[(user_id, str(unmatched_hash).lower())] = time.time()

            more = False
            ack_books = []
            for result in response.get("results") or []:
                doc_md5 = str(result.get("hash") or "").lower()
                if not doc_md5:
                    continue
                to_apply = result.get("toApply") or {}
                acks = self.database_service.apply_spoke_annotations(
                    user_id, doc_md5, SPOKE_KEY,
                    adds=to_apply.get("add") or [],
                    edits=to_apply.get("edit") or [],
                    deletes=to_apply.get("delete") or [],
                )
                # Upload bookkeeping: BookOrbit ingested our changes + processed
                # our key-omission deletions on receipt.
                self.database_service.mark_spoke_annotations_uploaded(
                    user_id, SPOKE_KEY,
                    annotation_ids=uploaded_ids_by_hash.get(doc_md5) or [],
                    tombstone_ids=tombstone_ids_by_hash.get(doc_md5) or [],
                )
                if acks["applied"] or acks["deleted"]:
                    ack_books.append({
                        "hash": doc_md5,
                        "applied": acks["applied"],
                        "deleted": acks["deleted"],
                    })
                if result.get("more"):
                    more = True

            if ack_books:
                client.koreader_exchange_annotations_ack(kosync_user, kosync_key, ack_books)

            applied_any = bool(ack_books)
            if applied_any:
                logger.info(
                    "📝 Annotation sync: applied BookOrbit changes for user %s (%d book(s))",
                    user_id, len(ack_books),
                )
            if not more:
                return


def run_annotation_sync_daemon(service: AnnotationSyncService, interval_getter) -> None:
    """Daemon loop: run a sync cycle every N minutes (0 disables)."""
    while True:
        try:
            interval_mins = interval_getter()
        except Exception:
            interval_mins = 0
        if interval_mins and interval_mins > 0:
            try:
                service.run_cycle()
            except Exception as e:
                logger.error("Annotation sync cycle crashed: %s", e, exc_info=True)
            time.sleep(max(60, int(interval_mins * 60)))
        else:
            time.sleep(300)
