import time
from collections import defaultdict
from datetime import date, datetime
from typing import Optional
from ..models import Book, KosyncDocument, KOReaderBookStatus
from src.utils.time_utils import utcnow
import logging

logger = logging.getLogger('src.db.database_service')

class KOReaderStatusMixin:
    """Database operations for koreader status."""

    @classmethod
    def _koreader_status_rank(cls, status: str) -> int:
        """Precedence rank for a KOReader status; unknown values sort lowest."""
        try:
            return cls.KOREADER_STATUS_PRECEDENCE.index(str(status or '').strip().lower())
        except ValueError:
            return 0

    @classmethod
    def _koreader_status_sort_key(cls, row) -> tuple:
        """Winner ordering for one book's per-device status rows.

        ``modified`` is the device's own date for the status change, so it is the
        closest thing to "when did the reader decide this" and leads the key.
        It is date-granularity and comes off two different device clocks, which is
        exactly why the precedence rank — not the second clock — breaks the tie.
        ``received_at`` is the bridge's single clock and only separates rows that
        agree on both date and status.
        """
        return (
            str(row.modified or ''),
            cls._koreader_status_rank(row.status),
            float(row.received_at or 0.0),
        )

    def upsert_koreader_book_status(
        self,
        device: str,
        device_id: str,
        books: list[dict],
        user_id: int = None,
        received_at: float = None,
    ) -> int:
        """Upsert one device's reported sidecar statuses. Returns rows accepted."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        device_key = self._normalize_koreader_device_key(device=device, device_id=device_id)
        if not device_key:
            return 0
        uid = self._resolve_uid(user_id)
        stamp = float(received_at if received_at is not None else time.time())

        rows = []
        seen: set[str] = set()
        for book in books or []:
            md5 = str(book.get("md5") or "").strip()
            status = str(book.get("status") or "").strip().lower()
            # An empty status carries no decision and must not outrank another
            # device's real one -- KOReader writes `status = ""` in the wild.
            if not md5 or not status or md5 in seen:
                continue
            seen.add(md5)
            modified = str(book.get("modified") or "").strip() or None
            rows.append({
                "md5": md5,
                "user_id": uid,
                "device": str(device or "").strip() or None,
                "device_id": str(device_id or "").strip() or None,
                "device_key": device_key,
                "status": status,
                "modified": modified,
                "received_at": stamp,
                "last_updated": utcnow(),
            })

        if not rows:
            return 0

        with self.get_session() as session:
            stmt = sqlite_insert(KOReaderBookStatus).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["md5", "user_id", "device_key"],
                set_={
                    "device": stmt.excluded.device,
                    "device_id": stmt.excluded.device_id,
                    "status": stmt.excluded.status,
                    "modified": stmt.excluded.modified,
                    "received_at": stmt.excluded.received_at,
                    "last_updated": utcnow(),
                },
            )
            session.execute(stmt)
        return len(rows)

    def record_koreader_status_for_book(
        self,
        abs_id: str,
        status: str,
        device_key: str,
        user_id: int = None,
        modified: str = None,
        device: str = None,
        only_if_absent: bool = False,
    ) -> int:
        """Record a bridge-originated status against every md5 linked to ``abs_id``.

        Used by sources that speak in books rather than document hashes (Clear
        Progress, the completion edge, the started edge). A book can have several
        sibling hashes -- different EPUB builds of the same title -- and each
        device knows only its own copy, so every linked hash gets the row.

        ``only_if_absent`` writes nothing for a hash that already has a status
        from any source. Inferring a status from POSITION must only fill a gap,
        never overrule a real decision: a book deliberately marked 'abandoned'
        half way through still has position, and would otherwise be dragged back
        to 'reading' on the next cycle, every cycle.

        Returns the number of hashes written.
        """
        abs_id = str(abs_id or '').strip()
        status = str(status or '').strip().lower()
        if not abs_id or not status:
            return 0

        hashes = []
        for doc in self.get_kosync_documents_for_book(abs_id):
            doc_hash = str(getattr(doc, 'document_hash', '') or '').strip()
            if doc_hash:
                hashes.append(doc_hash)
        if not hashes:
            return 0

        if only_if_absent:
            uid = self._resolve_uid(user_id)
            with self.get_session() as session:
                query = session.query(KOReaderBookStatus.md5).filter(
                    KOReaderBookStatus.md5.in_(hashes)
                )
                taken = {row.md5 for row in self._scope_koreader_user(
                    query, KOReaderBookStatus, uid).all()}
            hashes = [h for h in hashes if h not in taken]
            if not hashes:
                return 0

        stamp = modified or datetime.now().strftime('%Y-%m-%d')
        return self.upsert_koreader_book_status(
            device=device or device_key,
            device_id=device_key,
            books=[{"md5": h, "status": status, "modified": stamp} for h in hashes],
            user_id=user_id,
        )

    def resolve_koreader_book_status(
        self,
        user_id: int = None,
        md5s: Optional[set[str]] = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Resolve the cross-device winning status for each book.

        Returns ``[{md5, status, modified, source_device_key}]``. A device applies
        any entry whose status differs from its own sidecar; entries it already
        agrees with are a no-op there, so no per-device filtering happens here.

        A hash linked to a book but with no status of its own (a copy the reader has
        not opened yet) gets the book's winner. A copy that has its own status keeps
        it: a status inferred from position must only fill a gap, never overrule a
        decision made on another copy.
        """
        uid = self._resolve_uid(user_id)
        md5s = {str(m).strip() for m in (md5s or set()) if str(m).strip()}
        limit = max(min(int(limit or 5000), 5000), 1)

        # Select columns rather than ORM instances: the ranking below happens after
        # the session closes, and a detached instance would raise on attribute
        # access the moment it needed a refresh. The md5 filter is applied to the
        # result, not the query, because a requested copy can take its status from
        # a sibling copy's row.
        with self.get_session() as session:
            query = session.query(
                KOReaderBookStatus.md5,
                KOReaderBookStatus.status,
                KOReaderBookStatus.modified,
                KOReaderBookStatus.device_key,
                KOReaderBookStatus.received_at,
            )
            query = self._scope_koreader_user(query, KOReaderBookStatus, uid)
            rows = query.all()

            best: dict[str, tuple] = {}
            for row in rows:
                current = best.get(row.md5)
                if current is None or self._koreader_status_sort_key(row) > self._koreader_status_sort_key(current):
                    best[row.md5] = row

            hashes_by_book = self._linked_hashes_by_book(session, set(best))

        for hashes in hashes_by_book.values():
            book_best = max(
                (best[h] for h in hashes if h in best), key=self._koreader_status_sort_key, default=None,
            )
            if book_best is not None:
                for h in hashes:
                    best.setdefault(h, book_best)

        winners = sorted(
            ((md5, row) for md5, row in best.items() if not md5s or md5 in md5s),
            key=lambda item: item[0],
        )[:limit]
        return [
            {
                "md5": md5,
                "status": row.status,
                "modified": row.modified or "",
                "source_device_key": row.device_key,
            }
            for md5, row in winners
        ]

    @staticmethod
    def _linked_hashes_by_book(session, md5s: set[str]) -> dict[str, set[str]]:
        """``{abs_id: every hash linked to it}`` for the books any of ``md5s`` belongs to."""
        if not md5s:
            return {}
        abs_ids = {
            abs_id for (abs_id,) in session.query(KosyncDocument.linked_abs_id).filter(
                KosyncDocument.document_hash.in_(md5s), KosyncDocument.linked_abs_id.isnot(None),
            )
        } | {
            abs_id for (abs_id,) in session.query(Book.abs_id).filter(Book.kosync_doc_id.in_(md5s))
        }
        if not abs_ids:
            return {}
        hashes_by_book: dict[str, set[str]] = defaultdict(set)
        for doc_hash, abs_id in session.query(KosyncDocument.document_hash, KosyncDocument.linked_abs_id).filter(
            KosyncDocument.linked_abs_id.in_(abs_ids)
        ):
            hashes_by_book[abs_id].add(doc_hash)
        for kosync_doc_id, abs_id in session.query(Book.kosync_doc_id, Book.abs_id).filter(
            Book.abs_id.in_(abs_ids), Book.kosync_doc_id.isnot(None)
        ):
            hashes_by_book[abs_id].add(kosync_doc_id)
        return dict(hashes_by_book)
